# Home page (tiles)

`/` is now a Home page with one tile per page; the Analysis page moved from `/` to `/analysis.html` (same file, renamed).
- The tab bar (`static/auth.js`) gets a Home tab first. Home highlights on `/`, every other tab on its own page.
- The tiles are built from the tab bar itself, so they always match what the member may open (Rewards, Partner, admin tabs appear when their tabs do) and the Messages unread count shows on its tile.
- Plain-language captions and the icons live in `static/index.html` (`T` and `I`). A new tab only needs one line in `T` to get a caption and icon; without one it gets a default icon.
- Signed-out visitors: `/` still loads `track.js` (referral cookies `dl_pr`, `dl_rc`) and `auth.js` sends them to the login page, as before. After login everyone lands on Home.
- The web app manifest start URL, push notification links and the brand link in every header already point to `/` or `/signals.html`; nothing else needed changing.
