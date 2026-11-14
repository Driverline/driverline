# Admin alerts, chat-style Messages, partner queue (Nov 2026)

## Messages page (members): `static/inbox.html`, `app/inbox.py member_view`
- One chat: the team's replies, the member's own messages and announcements, **oldest at the top, newest at the bottom**, with day separators ("Today", "Yesterday", date) and a "New" divider before the first unread item.
- Opens **scrolled to the newest item**. Scrolling up loads older items (page of 40 messages and 15 announcements) and keeps the reading position.
- New items arrive by polling every 30 s (`after_msg` / `after_bc`). If the member has scrolled up, a "New messages" button appears instead of jumping.
- API: `GET /api/inbox` (newest page) | `?before_msg=&before_bc=` (older page; send 0 for a kind with nothing older) | `?after_msg=&after_bc=` (only what is new). Response keys unchanged, plus `more_messages` and `more_broadcasts`.
- Announcements are still stored once and shown to everyone (`inbox_broadcasts`).

## Admin alerts: `app/admin_alerts.py`, table `admin_alerts`, `migrations/2026_11_admin_alerts.sql`
Something an admin has to act on becomes one row (shown in Admin > Inbox, "Needs your attention", newest at the bottom, with a badge on the Inbox tab) and a push to every admin device that has push on.

| Alert | Raised when | Code |
|---|---|---|
| Payment: USDT sent | member submits a manual USDT payment | `credits.order_paid` |
| Payment: Naira transfer sent | member says they sent a Naira transfer | `naira.claim` |
| Payment: Crypto payment received | NOWPayments payment credited | `nowpay` fulfilment |
| MT5 details sent | member sends MT5 details for support to connect | `mt5_requests.create` |
| Copy trading switched on | a (non-admin) member switches copying on | `copy_trading` member settings |
| AutoTrading switched on | a (non-admin) member switches AutoTrading on | `autotrade.me_settings` |
| Partner application started / submitted | member starts or submits a Deriv partner application (non-admin) | `partners.api_become_start/submit` |

Rules: `notify()` never raises (it can never break a payment or a request); a repeat of the same event (same kind + ref) is ignored; alerts carry who, what and a link, **never** passwords, keys, tx ids or MT5 details. Read alerts older than 90 days are removed. Admins trying their own account raise no Copy trading / AutoTrading / partner alerts.
API: `GET /api/admin/alerts[?before=id]`, `POST /api/admin/alerts/read` (`{all:true}` or `{ids:[...]}`); `GET /api/admin/inbox/summary` now also returns `alerts`.
There is no member "request" form for Copy trading or AutoTrading, so those alerts fire when the member switches the feature on (Copy trading also needs a paid plan, which raises the payment alert).

## Partner queue: `static/admin-partners.html`
Each partner shows **Next: ...** (check in Partners Hub and Approve; add their trader link; verify the link). Partners needing action are listed first, with a count and a "needs action only" filter. Nothing is automatic: "verified" still means an admin checked by hand.

## Rollback
Revert the patch. The `admin_alerts` table is additive and can stay.
