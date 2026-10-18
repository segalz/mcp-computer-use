# Browser Test Fixtures

Test fixtures representing Chromium-based browsers (Google Chrome, Microsoft Edge).

## Origin & Classification

### Chrome Build Dashboard
- **`chrome_dashboard_snapshot.txt`**: Derived from `plan/raw-captures/chrome_snapshot_full.txt`.
- **`chrome_dashboard_interactive.txt`**: Derived from `plan/raw-captures/chrome_snapshot_dashboard.txt` (interactive-only capture).
- **`chrome_dashboard_text.txt`**: Derived from `plan/raw-captures/chrome_text_dashboard.txt`.

### Edge Network Error
- **`edge_error_snapshot.txt`**: Derived from `plan/raw-captures/edge_snapshot_example.txt`.

## Anonymization Applied
- User temp file paths (`C:/Users/...`) replaced with `C:/work/site/index.html` and `file:///C:/work/site/index.html`.
- Favourites bar bookmarks replaced with neutral `Example bookmark 1..6`.
- Personal and company email addresses replaced with `user@example.com`.
- Edge profile segment `Personal` replaced with `Profile 1`.
- Line structure, indentation, and node IDs preserved exactly, including zero-width space `\u200b` in Edge window titles.
