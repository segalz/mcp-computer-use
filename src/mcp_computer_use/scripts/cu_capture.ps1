[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TitleB64,

    [Parameter(Mandatory = $true)]
    [string]$OutPath,

    [int]$MaxWidth = 1024,

    [int]$Quality = 70
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

try {
    if (-not ([System.Management.Automation.PSTypeName]'McpComputerUse.Win32').Type) {
        $csharp = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

namespace McpComputerUse {
    public class Win32 {
        [StructLayout(LayoutKind.Sequential)]
        public struct RECT {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

        [DllImport("user32.dll", EntryPoint = "GetWindowTextW", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

        [DllImport("user32.dll", EntryPoint = "GetWindowTextLengthW", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern int GetWindowTextLengthW(IntPtr hWnd);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsWindowVisible(IntPtr hWnd);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool IsIconic(IntPtr hWnd);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

        [DllImport("dwmapi.dll")]
        public static extern int DwmGetWindowAttribute(IntPtr hwnd, int dwAttribute, out RECT pvAttribute, int cbAttribute);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);

        [DllImport("user32.dll")]
        public static extern IntPtr SetProcessDpiAwarenessContext(IntPtr dpiContext);

        public static bool IsBlank(IntPtr scan0, int stride, int width, int height) {
            if (width <= 0 || height <= 0 || scan0 == IntPtr.Zero) return true;
            int firstPixel = Marshal.ReadInt32(scan0);
            for (int y = 0; y < height; y++) {
                IntPtr row = new IntPtr(scan0.ToInt64() + (long)y * stride);
                for (int x = 0; x < width; x++) {
                    if (Marshal.ReadInt32(row, x * 4) != firstPixel) {
                        return false;
                    }
                }
            }
            return true;
        }
    }
}
"@
        Add-Type -TypeDefinition $csharp
    }

    Add-Type -AssemblyName System.Drawing

    # 1. DPI awareness first before any window lookup
    try {
        [McpComputerUse.Win32]::SetProcessDpiAwarenessContext([IntPtr](-4)) | Out-Null
    } catch {}

    # 2. Decode title
    $rawBytes = [System.Convert]::FromBase64String($TitleB64)
    $targetTitle = [System.Text.Encoding]::UTF8.GetString($rawBytes)

    # 3. Window lookup: EnumWindows + exact title equality (-ceq), visible top-level windows; fallback to process name main window
    $matchingWindows = [System.Collections.Generic.List[IntPtr]]::new()
    $enumCallback = [McpComputerUse.Win32+EnumWindowsProc]{
        param($hWnd, $lParam)
        if ([McpComputerUse.Win32]::IsWindowVisible($hWnd)) {
            $len = [McpComputerUse.Win32]::GetWindowTextLengthW($hWnd)
            if ($len -gt 0) {
                $sb = [System.Text.StringBuilder]::new($len + 1)
                [McpComputerUse.Win32]::GetWindowTextW($hWnd, $sb, $sb.Capacity) | Out-Null
                $title = $sb.ToString()
                if ($title -ceq $targetTitle) {
                    $matchingWindows.Add($hWnd)
                }
            }
        }
        return $true
    }
    [McpComputerUse.Win32]::EnumWindows($enumCallback, [IntPtr]::Zero) | Out-Null

    $targetHwnd = [IntPtr]::Zero

    if ($matchingWindows.Count -gt 0) {
        # Check for non-minimized window first
        foreach ($h in $matchingWindows) {
            if (-not [McpComputerUse.Win32]::IsIconic($h)) {
                $targetHwnd = $h
                break
            }
        }
        if ($targetHwnd -eq [IntPtr]::Zero) {
            # All matching windows are minimized
            [PSCustomObject]@{ ok = $false; code = "IMAGE_WINDOW_MINIMIZED"; message = "Target window is minimized" } | ConvertTo-Json -Compress
            exit 0
        }
    } else {
        # Fallback to process name main window only if no title match
        $procName = $targetTitle -replace '(?i)\.exe$', ''
        $escapedProc = [WildcardPattern]::Escape($procName)
        $procs = @(Get-Process -Name $escapedProc -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -ceq $procName })
        if ($procs.Count -eq 0) {
            $procs = @(Get-Process -Name $escapedProc -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -ieq $procName })
        }
        foreach ($p in $procs) {
            $h = $p.MainWindowHandle
            if ($h -ne [IntPtr]::Zero -and [McpComputerUse.Win32]::IsWindowVisible($h)) {
                if ([McpComputerUse.Win32]::IsIconic($h)) {
                    [PSCustomObject]@{ ok = $false; code = "IMAGE_WINDOW_MINIMIZED"; message = "Target window is minimized" } | ConvertTo-Json -Compress
                    exit 0
                } else {
                    $targetHwnd = $h
                    break
                }
            }
        }
        if ($targetHwnd -eq [IntPtr]::Zero) {
            throw [Exception]"Window not found: $targetTitle"
        }
    }

    # 4. Rect & DWM bounds
    $rect = [McpComputerUse.Win32+RECT]::new()
    if (-not [McpComputerUse.Win32]::GetWindowRect($targetHwnd, [ref]$rect)) {
        throw [Exception]"GetWindowRect failed"
    }

    $rectW = $rect.Right - $rect.Left
    $rectH = $rect.Bottom - $rect.Top
    if ($rectW -le 0 -or $rectH -le 0) {
        throw [Exception]"Window has invalid dimensions: ${rectW}x${rectH}"
    }

    # DWMWA_EXTENDED_FRAME_BOUNDS = 9
    $dwmRect = [McpComputerUse.Win32+RECT]::new()
    $dwmSize = [System.Runtime.InteropServices.Marshal]::SizeOf([type][McpComputerUse.Win32+RECT])
    $hr = [McpComputerUse.Win32]::DwmGetWindowAttribute($targetHwnd, 9, [ref]$dwmRect, $dwmSize)

    $cropX = 0
    $cropY = 0
    $cropW = $rectW
    $cropH = $rectH

    if ($hr -eq 0) {
        $candX = $dwmRect.Left - $rect.Left
        $candY = $dwmRect.Top - $rect.Top
        $candW = $dwmRect.Right - $dwmRect.Left
        $candH = $dwmRect.Bottom - $dwmRect.Top
        if ($candX -ge 0 -and $candY -ge 0 -and $candW -gt 0 -and $candH -gt 0 -and ($candX + $candW) -le $rectW -and ($candY + $candH) -le $rectH) {
            $cropX = $candX
            $cropY = $candY
            $cropW = $candW
            $cropH = $candH
        }
    }

    # 5. Capture, Crop, Downscale, Blank Check, and Save in guarded block with finally cleanup
    $fullBmp = $null
    $croppedBmp = $null
    $finalBmp = $null
    try {
        $fullBmp = [System.Drawing.Bitmap]::new($rectW, $rectH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $g = [System.Drawing.Graphics]::FromImage($fullBmp)
        try {
            $hdc = $g.GetHdc()
            try {
                $pwSuccess = [McpComputerUse.Win32]::PrintWindow($targetHwnd, $hdc, 2)
                if (-not $pwSuccess) {
                    $pwSuccess = [McpComputerUse.Win32]::PrintWindow($targetHwnd, $hdc, 0)
                }
            } finally {
                $g.ReleaseHdc($hdc)
            }
        } finally {
            $g.Dispose()
        }

        if (-not $pwSuccess) {
            throw [Exception]"PrintWindow failed"
        }

        # Crop to DWM bounds offset
        if ($cropX -eq 0 -and $cropY -eq 0 -and $cropW -eq $rectW -and $cropH -eq $rectH) {
            $croppedBmp = $fullBmp
        } else {
            $croppedBmp = [System.Drawing.Bitmap]::new($cropW, $cropH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $cg = [System.Drawing.Graphics]::FromImage($croppedBmp)
            try {
                $srcRect = [System.Drawing.Rectangle]::new($cropX, $cropY, $cropW, $cropH)
                $destRect = [System.Drawing.Rectangle]::new(0, 0, $cropW, $cropH)
                $cg.DrawImage($fullBmp, $destRect, $srcRect, [System.Drawing.GraphicsUnit]::Pixel)
            } finally {
                $cg.Dispose()
            }
        }

        # Downscale to <= MaxWidth (no upscale) with HighQualityBicubic
        if ($cropW -le $MaxWidth) {
            $finalBmp = $croppedBmp
            $finalW = $cropW
            $finalH = $cropH
        } else {
            $finalW = $MaxWidth
            $finalH = [int][Math]::Round([double]$cropH * $MaxWidth / $cropW)
            if ($finalH -lt 1) { $finalH = 1 }

            $finalBmp = [System.Drawing.Bitmap]::new($finalW, $finalH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
            $fg = [System.Drawing.Graphics]::FromImage($finalBmp)
            try {
                $fg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $fg.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
                $fg.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $fg.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
                $fg.DrawImage($croppedBmp, 0, 0, $finalW, $finalH)
            } finally {
                $fg.Dispose()
            }
        }

        # Blank check via LockBits on downscaled bitmap
        $chkRect = [System.Drawing.Rectangle]::new(0, 0, $finalW, $finalH)
        $lockData = $finalBmp.LockBits($chkRect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $isBlank = $false
        try {
            $isBlank = [McpComputerUse.Win32]::IsBlank($lockData.Scan0, $lockData.Stride, $finalW, $finalH)
        } finally {
            $finalBmp.UnlockBits($lockData)
        }

        if ($isBlank) {
            [PSCustomObject]@{ ok = $false; code = "IMAGE_CAPTURE_BLANK"; message = "Captured image is blank" } | ConvertTo-Json -Compress
            exit 0
        }

        # Save JPEG quality
        $jpegCodec = [System.Drawing.Imaging.ImageCodecInfo]::GetImageEncoders() | Where-Object { $_.MimeType -eq 'image/jpeg' } | Select-Object -First 1
        if (-not $jpegCodec) {
            throw [Exception]"JPEG encoder not found"
        }

        $encoderParams = [System.Drawing.Imaging.EncoderParameters]::new(1)
        $encoderParams.Param[0] = [System.Drawing.Imaging.EncoderParameter]::new([System.Drawing.Imaging.Encoder]::Quality, [long]$Quality)

        $finalBmp.Save($OutPath, $jpegCodec, $encoderParams)

        # Print one JSON line
        $fileLen = (Get-Item -LiteralPath $OutPath).Length
        [PSCustomObject]@{ ok = $true; width = $finalW; height = $finalH; bytes = $fileLen } | ConvertTo-Json -Compress
        exit 0
    } finally {
        if ($finalBmp -and -not [object]::ReferenceEquals($finalBmp, $croppedBmp) -and -not [object]::ReferenceEquals($finalBmp, $fullBmp)) {
            $finalBmp.Dispose()
        }
        if ($croppedBmp -and -not [object]::ReferenceEquals($croppedBmp, $fullBmp)) {
            $croppedBmp.Dispose()
        }
        if ($fullBmp) {
            $fullBmp.Dispose()
        }
    }
}
catch {
    $shortMsg = $_.Exception.Message -replace '[\r\n]', ' '
    if ($shortMsg.Length -gt 150) {
        $shortMsg = $shortMsg.Substring(0, 150)
    }
    [PSCustomObject]@{ ok = $false; code = "IMAGE_CAPTURE_FAILED"; message = $shortMsg } | ConvertTo-Json -Compress
    exit 0
}
