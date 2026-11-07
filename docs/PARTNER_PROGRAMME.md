# Partner referral programme, and the retirement of commission sharing (Nov 2026)

**Status: built to be reviewed by Deriv. It is OFF.** Nothing is public, no link is handed to the public, nobody is told TradeLens has Deriv's approval.

## The rule that decides everything
Deriv alone decides partner approval, client tagging, partner relationships and partner commissions. TradeLens only keeps:
its own record of who invited whom, which Deriv referral link belongs to which TradeLens partner, and the consents a member gave before being sent to Deriv.
A TradeLens row is never proof of Deriv attribution. There is no Deriv API or webhook in `app/partners.py` (a test checks it makes no network call and never writes to `deriv_accounts`).
Every "verified" flag means **an admin checked it by hand** and recorded where (for example Deriv Partners Hub) and on what date.

## What changed
| | Before | Now |
|---|---|---|
| Commission sharing (10% of Deriv commission to referrers, balances, USDT payouts, import) | live code, 0 rows | **retired**. Code removed. The four tables are kept (read-only history); a database trigger refuses any new row |
| Points | included 50 points when a referral linked Deriv, and points for trades placed through the Deriv button | those two removed. All other points rules, balances and history unchanged |
| Referral qualifies when | the friend linked a Deriv account | the friend ran their first AI analysis (TradeLens onboarding). No points for it |
| Referral purchase rewards (5% signals, free connection) | off unless `REWARDS_BONUS_ON=1` | cannot be switched on (`bonus_enabled()` is always False). Existing grants untouched |
| Partner bonus signals and founding-member MT5 perk | granted automatically | no new grants unless `PARTNER_PERKS_ON=1` (leave unset). Existing grants kept |
| Universal Deriv link `deriv_ib_link` (Create-account link on the login page, Deriv sign-in) | shown to everyone | hidden until the programme is **public** |
| New | | Master Partner -> sub-partners -> referred members (below) |

Points and Deriv commission are completely separate. Nothing in the points code mentions Deriv, and no points are given for Deriv sign-ups, recruiting partners, volume, performance or commission.

## Modes (Admin > Partners > Mode)
- **off** (default): admins only; no referral is recorded; the old universal link is hidden.
- **review**: admins and the reviewer accounts you list can use every screen. Reviewers get a read-only admin view with member e-mails masked.
- **public**: refused until Deriv's written approval and conditions are recorded in the same screen. Stage 2 only.

## Attribution policy
First valid touch at sign-up wins. `?ref=CODE` is kept 30 days by `static/track.js` (first touch wins), checked on the server when the account is created (partner must be approved; unknown, malformed, inactive and own codes are logged and ignored), stored once (`referred_id` is the primary key) and never overwritten by a later link. Sharing URL: `https://tradelens360ai.live/login.html?ref=TLxxxxxx`. The older `?rc=` points links keep working.

## Journeys
- **A, Trader:** disclosure + two ticks -> the introducing partner's verified Deriv link. If that partner is not approved or the link not verified: blocked, with an explanation. **The Master Partner link is never a fallback.** A member nobody introduced uses your own "house" record (approved and link-verified like any partner).
- **B, Become a partner:** disclosure + consent -> the Master Partner link from Admin settings (or "already tagged under TradeLens: skip it", as Deriv confirmed). Status stays *Awaiting Deriv approval* until an admin has verified it in Deriv Partners Hub. Then: approve (needs Deriv partner ID, source, date) -> add the partner's Deriv trader link -> mark link verified -> the partner's referral code goes live.

## Tables (all new, RLS on, no policies; `migrations/2026_11_partners.sql`)
`tl_partners`, `tl_partner_referrals`, `tl_partner_events`, `tl_partner_consents`, `tl_partner_verifications`, `tl_partner_audit`, `tl_partner_settings`.

## Giving Deriv reviewers access
1. Reviewers need a normal TradeLens account (invite code). Create invites in Admin as usual.
2. Admin > Partners: set **Mode = Review**, add their e-mails under *Reviewer accounts*, Save.
3. Reviewers see a **Partner** tab: disclosures, the trader hand-off, Become a partner, the partner dashboard, and a link to the read-only admin view. They cannot change anything and see masked e-mails.
4. To let them test the hand-offs end to end, create two test members and a test partner in review mode. Their hand-offs open Deriv's own links in a new tab and are recorded as review activity.
5. When finished: Mode = Off, remove the reviewer e-mails.

## Going public (Stage 2, only after Deriv's written approval)
Record the approval and its conditions in Admin > Partners (required), apply any conditions, set Mode = Public. Keep manual verification until Deriv confirms an authorised automated mechanism. Rollback: Mode = Off.

## Benefits tied to Deriv tagging: CLOSED for the Deriv review (Nov 2026)
Unless `PARTNER_PERKS_ON=1` is set in Render (leave it unset), all of these are off: the Signals Pro price of $15 for tagged members (`perks.price_for`), the Copy trading mode of Signals Plus (`copy_plan.ib_linked`, and the note on the Billing page), new partner bonus signals, and new founding-member places. Nothing already granted is taken back. On 7 Nov 2026 nobody held an active Plus or Pro plan, so no paying member was affected.

## Privacy and consent
`static/privacy.html` is the public Privacy Notice (data collected, purposes, recipients such as Anthropic, NOWPayments, Telegram, Render/Supabase, cookies, retention, rights). It is linked from the sign-in page, the Terms, and both consent screens. Consent records carry the notice version (`2026-11-v2`); consent to an older version is refused. Have it reviewed by a lawyer before relying on it.

## Existing Deriv API check
`app/partner.py` can call Deriv's Partners API (`client-tags/check`) when an admin triggers it, or automatically if `DERIV_PARTNER_TOKEN` is set. On 7 Nov 2026 `DERIV_PARTNER_TOKEN`, `PARTNER_GATE` and `DERIV_SIGNUP_VIA_OAUTH` were NOT set in Render. Do not press the check button in Admin while Deriv's review is open. It was disclosed to Deriv.

## Open decisions
- `deriv_trades.markup_pct` (TradeLens earns a markup on Deriv-button trades) is not changed here.
- Login page "Verify your Deriv account" card: the wording no longer mentions free welcome signals. The 3 welcome signals are given to every new member (`credits.ensure_welcome`), so no behaviour changed. It can be switched off with "Deriv enabled" in Admin > Payments.

## Deriv Partners API check: off by default

`partner.api_on()` is false unless `DERIV_PARTNER_API_ON=1`. While it is off, `check_clients` refuses before any request is made, the admin
"Check with Deriv" sign-in and the saved-token check return 403, and the admin page says verification is by hand. Leave it unset during any Deriv review.
Separately, `DERIV_CAPTURE_CLIENT_ID` (default on) reads a member's Deriv client ID from Deriv's userinfo endpoint at Deriv sign-in; set it to `0` to stop that.
