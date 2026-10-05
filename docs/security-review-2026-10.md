# Security and hardening review — `local_payments`

| | |
| --- | --- |
| Date | 2026-10-05 |
| Repository reviewed | `skyengpro/local_payments`, branch `lp-11-finalize-erpnext-payment-requests-after-a-local-payment`, commit `a5a3b8e` |
| Scope | The `local_payments` app only: `api.py`, `gateway.py`, `reconcile.py`, `lifecycle.py`, `erpnext.py`, `scheduler.py`, `hooks.py`, `providers/`, its doctypes, its role fixture and the checkout page |
| Other apps | `frappe`, `erpnext` and `payments` (v16, maintained by Frappe) were read only to see how they call this app and react to it. No item asks to change them. |
| Status | Plan agreed on 2026-10-05. Nothing is implemented yet. |

This document lists the security weaknesses and robustness gaps that the current
code does not address yet, plus the improvements found while investigating the
2026-10-05 incident on `acad.localhost` (two MTN MoMo payments received, Payment
Requests left `Requested`). Each item states what is wrong, where, why it matters
and what to do. The design decisions in [`ARCHITECTURE.md`](ARCHITECTURE.md)
(D1–D8) and the ADRs are taken as given. Where an item would go beyond them, it
says so and asks for an ADR.

## Where fixes live

`local_payments` is the only codebase we change. Each item says where its fix
lives:

- **App**: code, doctype JSON, fixtures or hooks of `local_payments`. Behaviour
  on other apps' doctypes goes through our own `hooks.py` (`doc_events`), never
  through a change to their code.
- **Deployment**: reverse proxy, DNS, TLS certificate, site config, Redis. It is
  set when a site is deployed and recorded in ARCHITECTURE "Installation and
  configuration".
- **Process**: decisions or checks outside the code (legal review, MTN Cameroon
  go-live questions, assigning roles to people).

## Summary

Severity reflects impact on money, personal data or availability, weighted by
how reachable the weakness is today.

| ID | Title | Severity | Area | Fix lives in |
| --- | --- | --- | --- | --- |
| [LP-SEC-01](#lp-sec-01-payment-account-not-validated-money-collected-but-not-booked) | Payment account not validated: money collected but not booked | **High** | ERPNext integration | App |
| [LP-SEC-16](#lp-sec-16-alerts-are-lost-when-nobody-holds-local-payments-manager) | Alerts are lost when nobody holds Local Payments Manager | **High** | Roles / operations | App + process |
| [LP-SEC-02](#lp-sec-02-settlement-does-not-compare-the-paid-amount-with-the-payment-request) | Settlement does not compare the paid amount with the Payment Request | Medium | ERPNext integration | App |
| [LP-SEC-15](#lp-sec-15-session-record-fields-can-be-edited-after-creation) | Session record fields can be edited after creation | Medium | Local Payment doctype | App |
| [LP-SEC-03](#lp-sec-03-payer-phone-numbers-written-to-the-error-log) | Payer phone numbers written to the Error Log | Medium | Personal data | App |
| [LP-SEC-04](#lp-sec-04-no-retention-limit-on-payer-personal-data) | No retention limit on payer personal data | Medium | Personal data | App + process |
| [LP-SEC-05](#lp-sec-05-checkout-tokens-never-expire) | Checkout tokens never expire | Medium | Guest surface | App + deployment |
| [LP-SEC-06](#lp-sec-06-rate-limits-trust-the-first-x-forwarded-for-entry) | Rate limits trust the first `X-Forwarded-For` entry | Medium | Guest surface | Deployment |
| [LP-SEC-07](#lp-sec-07-callback-url-depends-on-host-configuration-and-is-not-forced-to-https) | Callback URL depends on host configuration and is not forced to HTTPS | Medium | Transport | App + deployment |
| [LP-SEC-08](#lp-sec-08-a-token-holder-can-push-payment-prompts-to-any-number) | A token holder can push payment prompts to any number | Low | Guest surface | App |
| [LP-SEC-09](#lp-sec-09-exit-url-accepts-protocol-relative-and-plain-http-urls) | Exit URL accepts protocol-relative and plain-HTTP URLs | Low | Guest surface | App |
| [LP-SEC-10](#lp-sec-10-production-settings-accept-any-https-host) | Production settings accept any HTTPS host | Low | Configuration | App |
| [LP-SEC-11](#lp-sec-11-outbound-tls-relies-on-library-defaults) | Outbound TLS relies on library defaults | Low | Transport | App |
| [LP-SEC-12](#lp-sec-12-callback-endpoint-accepts-any-source) | Callback endpoint accepts any source | Low | Guest surface | Deployment + process |
| [LP-SEC-13](#lp-sec-13-security-headers-on-the-checkout-page-are-not-defined) | Security headers on the checkout page are not defined | Low | Guest surface | Deployment |
| [LP-SEC-17](#lp-sec-17-session-token-shown-in-the-desk) | Session token shown in the desk | Low | Local Payment doctype | App |
| [LP-SEC-14](#lp-sec-14-provider-access-token-cached-in-clear-in-redis) | Provider access token cached in clear in Redis | Info | Secrets | Deployment |
| [LP-IMP-01](#lp-imp-01-retry-authorization-button-on-each-session) | "Retry authorization" button on each session | Improvement | Operations | App |
| [LP-IMP-02](#lp-imp-02-alert-on-the-first-non-transient-authorization-failure) | Alert on the first non-transient authorization failure | Improvement | Operations | App |
| [LP-IMP-03](#lp-imp-03-payment-requests-created-with-a-bad-account-prevent-do-not-repair) | Payment Requests created with a bad account: prevent, do not repair | Improvement | Operations | App + process |
| [LP-IMP-04](#lp-imp-04-permissions-of-local-payments-manager) | Permissions of Local Payments Manager | Improvement | Roles | App |
| [LP-IMP-05](#lp-imp-05-local-payments-workspace) | Local Payments workspace | Improvement | Usability | App |

TLS certificate requirements are summarised in
[Transport and TLS certificates](#transport-and-tls-certificates).

## Suggested order

1. **Before any production payment**: LP-SEC-01, LP-SEC-16, LP-SEC-15,
   LP-IMP-01, LP-IMP-04, and the deployment items LP-SEC-06, LP-SEC-07 (site
   config part) and LP-SEC-13, plus the TLS certificate.
2. **Next**: LP-SEC-02, LP-SEC-03, LP-SEC-07 (app part), LP-SEC-10, LP-IMP-02,
   LP-IMP-03.
3. **Then**: LP-SEC-04, LP-SEC-05, LP-SEC-08, LP-SEC-09, LP-SEC-11, LP-SEC-17,
   LP-IMP-05, and the go-live questions to MTN Cameroon (LP-SEC-12, TLS table).

---

## ERPNext integration

### LP-SEC-01. Payment account not validated: money collected but not booked

**Severity: High.** Happened on 2026-10-05.

**What happens.** Nothing checks that the `payment_account` of a
`Payment Gateway Account` pointing to one of our gateways can receive postings.
ERPNext's own `PaymentGatewayAccount.validate()` only copies the account currency
(`erpnext/accounts/doctype/payment_gateway_account/payment_gateway_account.py`).
The Payment Request copies `payment_account` from the gateway account when it is
created, and uses it only when `set_as_paid()` creates the Payment Entry. That
happens after the payer has paid.

**Incident.** The gateway account `MTN MoMo-MTN - XAF - SEP` pointed to
`1 - Comptes de ressources durables - SEP`, a group account. Sessions
`LPAY-2026-00022` and `LPAY-2026-00023` moved to `Paid`. `on_payment_authorized`
called `set_as_paid()` ([`erpnext.py:74`](../local_payments/erpnext.py#L74)). The
Payment Entry submission failed with *"is a Group Account and group accounts
cannot be used in transactions"*, the transaction rolled back, and the Payment
Requests stayed `Requested`. The payer's money was with the merchant, and nothing
was booked.

A non-group account of the wrong type also passes. During the fix, an expense
account (`6011`, root type Expense) was selected first. That would have booked
receipts as expenses without any error.

**Recommendations.**

1. **Validate the gateway account.** Add a `validate` doc event on
   `Payment Gateway Account`, active only when `payment_gateway` is one of ours
   (same test as `_is_ours()`). Refuse an account that is:
   - a group (`is_group = 1`), or disabled;
   - not of `account_type` `Bank` or `Cash` (root type `Asset`);
   - in another company than the gateway account's `company`;
   - in another currency than the Settings document's `currency`.
2. **Check before the payer pays.** In `get_payment_url()`
   ([`gateway.py:102`](../local_payments/gateway.py#L102)), when
   `reference_doctype == "Payment Request"`, run the same check on the Payment
   Request's own `payment_account` and refuse to create the session if it fails.
   This is the check that matters most. ERPNext calls `get_payment_url()` when
   the Payment Request is submitted (`payment_request.py:303`), so a request
   whose account cannot take postings **cannot be submitted at all**. The
   accounting user sees the error before any link is sent and before money
   moves. `gateway.py` must keep working without ERPNext, so read the fields
   with `frappe.db.get_value` and skip the check when the doctype does not
   exist. `start_attempt` should run the same check again before pushing a
   request, for links issued before this change.
3. Keep ERPNext's error text in `authorization_error`, but prefix it with a
   readable cause when the failure is an accounting validation, so the desk
   shows "Payment account is not postable" instead of a raw ledger message.

Tests: a site test per refused account shape, and a test showing that
`get_payment_url()` refuses a Payment Request whose account is a group.

### LP-SEC-02. Settlement does not compare the paid amount with the Payment Request

**Severity: Medium.** Defence in depth. No exploit path found today.

**What happens.** [`erpnext.on_payment_authorized`](../local_payments/erpnext.py#L52)
settles a Payment Request as soon as *any* `Local Payment` that references it is
`Paid`. `reconcile()` compares the provider's confirmed amount with the
**session** amount, but nothing compares the session amount with the Payment
Request's `grand_total`, or the session currency with the request currency.
`get_payment_url()` trusts whatever `amount` the caller passes.

**Why it matters.** Today the Payment Request passes its own `grand_total`, and
the guest endpoint `payments.utils.get_checkout_url` cannot load a non-Single
Settings doctype: `frappe.get_doc("MTN MoMo Settings")` raises
`DoesNotExistError` (checked on `acad.localhost`). But any future consumer, guest
route or `payments` change that lets a caller choose `amount` for a
`Payment Request` reference would let someone pay 1 XAF and settle an invoice of
any size.

**Recommendation.** In `on_payment_authorized`, under the lock, read the paid
session(s) and refuse (raise, so authorization goes to `Failed` and is alerted)
unless `paid_amount == grand_total` and `currency == request.currency`, compared
exactly as `lifecycle.amounts_match` does. Optionally also check in
`get_payment_url()` that a `Payment Request` reference is submitted and that the
`amount` passed equals its `grand_total`.

---

## Local Payment doctype and roles

### LP-SEC-15. Session record fields can be edited after creation

**Severity: Medium.**

**What happens.** A `Local Payment` records what was asked for and what the
provider confirmed. Its roles have read and report only (no Custom DocPerm or
Property Setter on `acad.localhost`), but Administrator holds every permission
(checked with `get_doc_permissions`). These fields are not `read_only` and can be
edited and saved from the form: `payment_gateway`, `reference_doctype`,
`reference_docname`, `amount`, `currency`, `title`, `description`, `payer_name`,
`payer_email`, `redirect_to`. `read_only` only binds the form: Frappe does not
enforce it on the server, so the REST API can change `status` or `paid_amount`
as well. `track_changes` is off, so such an edit leaves no trace.

**Why it matters.** `reconcile()` compares the provider's confirmed amount with
the session's `amount`. Lowering `amount`, or pointing `reference_docname` at
another Payment Request, changes what a provider confirmation settles (see also
LP-SEC-02).

**Recommendation.**

1. **Fields fixed at creation**: `token`, `payment_gateway`, `reference_doctype`,
   `reference_docname`, `amount`, `currency`, `title`, `description`,
   `payer_name`, `payer_email`, `redirect_to`, `request_data`. Mark them
   `read_only` for the form and `set_only_once` for the server. Frappe then
   refuses any change after insert with `CannotChangeConstantError`, for every
   user including Administrator (`frappe/model/document.py`,
   `validate_set_only_once`, which has no user exception). No app code changes
   these fields after insert, so nothing breaks.
2. **State fields** (`status`, `paid_amount`, `paid_on`,
   `provider_transaction_id`, `authorization*`, `success_redirect`, `attempts`):
   `reconcile.py` and `erpnext.void_open_sessions` change them legitimately. In
   `LocalPayment.validate()`, refuse a change to them unless the save carries a
   flag set by that code (for example `doc.flags.from_local_payments`).
3. Turn on `track_changes` on `Local Payment`.

Tests: a site test per group (an Administrator edit is refused, a `reconcile()`
save goes through) and a test that a REST `PUT` on `status` is refused.

### LP-SEC-16. Alerts are lost when nobody holds Local Payments Manager

**Severity: High.** Confirmed on `acad.localhost`.

**What happens.** The role is shipped by a fixture
([`hooks.py:255`](../local_payments/hooks.py#L255)), with desk access, but is
assigned to nobody by default. It is the only recipient of
`alert_managers()` ([`reconcile.py:325`](../local_payments/reconcile.py#L325)):
duplicate payment, amount or currency mismatch, payment received on a voided
session, attempt unresolved after 72 h, authorization given up. Checked on
`acad.localhost`: no user holds it, and no active user other than Administrator
holds System Manager.

- `get_users_with_role()` excludes Administrator and disabled users
  (`frappe/utils/user.py`). With no holder, `alert_managers()` loops over an
  empty list. No notification, no error, no Error Log.
- `_alert_authorization` and `_alert_unresolved`
  ([`scheduler.py`](../local_payments/scheduler.py)) set `authorization_alerted`
  or `alerted` **before** sending. The alert is marked as sent and never retried,
  even once the role is assigned later.
- The alerts are `Notification Log` rows of type `Alert`, and Frappe never emails
  that type (`notification_settings.py`, `is_email_notifications_enabled_for_type`).
  They show only in the desk notification bell.
- System Manager receives nothing either.

The cases that need a person, such as a duplicate payment to refund or a payment
received but not booked, can therefore go unnoticed for good.

**Recommendation.** Assigning the role to a person is site data, so it can't
ship with the app. CLAUDE.md requires the role itself to ship as a fixture,
which it does. The app can make a missing holder visible:

1. **Fallback recipients.** With no active holder, `alert_managers()` notifies
   active System Managers. With none of those either, it writes an Error Log
   ("Local Payment alert with no recipient") carrying the alert text.
2. **Flag after delivery.** Set `alerted` / `authorization_alerted` only once
   at least one notification was inserted, so the daily job tries again.
3. **Configuration warning.** Saving an enabled `MTN MoMo Settings` in
   Production with no active Local Payments Manager shows a warning.
4. **Documentation.** Add "assign Local Payments Manager to at least one person"
   to ARCHITECTURE "Installation and configuration".
5. Optionally, also email the alert through an `Email Account` when one is set
   up, since a bell notification is easy to miss.

### LP-SEC-17. Session token shown in the desk

**Severity: Low.**

**What happens.** `Local Payment.token` is visible on the form to anyone who can
read the session. Holding it opens the checkout page and lets the holder start
attempts (LP-SEC-08).

**Why the token is stored at all.** It is the key that maps a link to its session:
`find_session()` looks sessions up by token
([`api.py:76`](../local_payments/api.py#L76)), and `_find_open_session()` reuses
an open session and rebuilds its URL from the stored token
([`gateway.py:119`](../local_payments/gateway.py#L119)). Storing only a SHA-256
hash would still allow the lookup, since the token carries 128 bits of entropy.
But reuse would then need a new token on each call, which would kill links
already sent. The gain would also be small: ERPNext already stores the full URL,
token included, in `Payment Request.payment_url` (checked on
`ACC-PRQ-2026-00001`), readable by every Accounts User, and sends it by email.

**Recommendation.** Keep storing the token. Hide it from the form (`hidden`, or
`permlevel` 1 with no role granted at that level), and make it `set_only_once`
(LP-SEC-15). Hashing is not planned while `payment_url` holds the token on the
ERPNext side.

---

## Personal data

Cameroon's personal data protection law (Law No. 2024/017 of 23 December 2024)
came into force on 23 June 2026, after an 18-month compliance period. It created
a data protection authority and restricts transfers of Cameroonian residents'
data outside the country. *Sources: Cameroonian and African press (scope:
Cameroon), see [References](#references). The official text in the Journal
Officiel has not been read for this review. Have it checked by legal counsel.*
Payer phone numbers are personal data under that law.

### LP-SEC-03. Payer phone numbers written to the Error Log

**Severity: Medium.**

**What happens.** When a payer types a number that `payer_msisdn()` rejects,
`start_attempt` raises a `ValidationError`. Frappe stores the request in the
Error Log as a traceback *with variables*. Its sanitizer masks variables whose
names contain `token`, `key`, `secret` or `password`
(`frappe/utils/__init__.py`, `_get_traceback_sanitizer`), but not `msisdn` or
`number`. Checked on `acad.localhost`: Error Log `454bqr8auh` contains the typed
number in `kwargs`, `msisdn` and `number`. Each mistyped number, up to 30 per
address every 10 minutes, adds such an entry. This breaks ARCHITECTURE
"Security and permissions" (the number is visible only to the roles above) and
the guest-surface rule "detail goes to the error log without … the full MSISDN".

**Recommendation.** For an invalid number, don't raise an exception that Frappe
logs: answer the expected validation failure without going through the error
logger. One way is to return a structured result
(`{"error": "invalid_msisdn", "message": …}`) and set
`frappe.local.response.http_status_code = 417` (Expectation Failed). The page
already reads the message, so the change there is small. Then add a site test
that posts an invalid number and asserts that no Error Log row contains it.
Purge the existing entries.

### LP-SEC-04. No retention limit on payer personal data

**Severity: Medium.**

**What happens.** The payer's number is kept forever in:
- `Local Payment Attempt.payer_msisdn` (also shown in the attempts grid);
- `Integration Request.data` (the RequestToPay body carries `payer.partyId`);
- Error Log entries (LP-SEC-03);
- `Local Payment.request_data` (payer name and email from the consumer).

**Recommendation.** Define a retention period with the data owner. Add a daily
job that, for sessions settled (`Paid` with `authorization = Done`, or `Void`)
for longer than that period, masks `payer_msisdn` to its last 3 digits, drops
`partyId` from the linked Integration Requests, and clears payer fields from
`request_data`. Document the period in ARCHITECTURE. Also check where the site
is hosted: hosting outside Cameroon may need the authority's prior approval
under the new law.

---

## Guest surface

### LP-SEC-05. Checkout tokens never expire

**Severity: Medium.**

**What happens.** The 32-hex token (128 bits, `secrets.token_hex`) is the only
key to a session, and it travels in the URL
([`gateway.py:117`](../local_payments/gateway.py#L117)). A session stays `Open`
until it is paid or its Payment Request is cancelled. The token therefore opens
the checkout page indefinitely. URLs end up in reverse proxy access logs, browser
history, chat and email forwards. `<meta name="referrer" content="no-referrer">`
already keeps it out of `Referer` headers.

**Recommendation.**
- Give sessions a validity period (configurable in the Settings doctype, for
  example 7 days), after which `start_attempt` refuses and the page shows
  "expired, ask for a new link". `get_payment_url()` already reuses an open
  session, so issuing a new link means voiding the expired session and creating
  a new one with a new token.
- Make sure the reverse proxy does not log query strings for
  `/local_payment_checkout` and `/api/method/local_payments.api.*`, or masks the
  `token` parameter.

### LP-SEC-06. Rate limits trust the first `X-Forwarded-For` entry

**Severity: Medium.** Depends on deployment.

**What happens.** Every per-address `rate_limit` on the guest endpoints keys on
`frappe.local.request_ip`. Frappe takes it from the **first** value of
`X-Forwarded-For` when the header is present (`frappe/auth.py`,
`set_request_ip`). Two failure modes:
- If the edge proxy appends to a client-supplied `X-Forwarded-For` instead of
  overwriting it, an attacker sets a new value on each request and bypasses
  `STARTS_PER_ADDRESS`, `POLLS_PER_ADDRESS` and `CALLBACKS_PER_ADDRESS`.
- If a load balancer or tunnel in front of nginx does not forward the client
  address, every payer shares one address, and 30 starts per 10 minutes become
  a site-wide limit.

**Recommendation.** For each production topology, check that the outermost
trusted proxy overwrites `X-Forwarded-For` with the real client address. Frappe's
nginx template does this with `$remote_addr`, but an extra CDN or load balancer
in front changes it. Add this check to "Installation and configuration" in
ARCHITECTURE. The per-token limits are not affected.

### LP-SEC-08. A token holder can push payment prompts to any number

**Severity: Low.**

**What happens.** Anyone holding a checkout link can enter any valid number.
MTN then pushes a payment prompt in the merchant's name to that phone, up to
`STARTS_PER_TOKEN` (10) times per 10 minutes, without limit over time. It can be
used to spam phones or to trick a third party into approving someone else's
invoice.

**Recommendation.** Cap the number of *distinct* numbers per session (for
example 3), and add a daily cap per token on top of the 10-minute window.

### LP-SEC-09. Exit URL accepts protocol-relative and plain-HTTP URLs

**Severity: Low.**

**What happens.** `SAFE_URL = r"\A(/|https?://)"`
([`api.py:50`](../local_payments/api.py#L50)) is meant to accept a path on this
site or an absolute address. `//evil.example` starts with `/` and passes, and
browsers treat it as an absolute URL to another host. `http://` URLs pass too.
`redirect_to` and `success_redirect` come from consumers, which set them
server-side today, so this is an open redirect only if a consumer forwards user
input.

**Recommendation.** Accept a path only if it starts with a single `/` followed by
neither `/` nor `\`. Accept an absolute URL only with `https://` and a host equal
to the site's host or listed in a setting. Otherwise fall back to the default
success page.

### LP-SEC-12. Callback endpoint accepts any source

**Severity: Low.**

**What happens.** `mtn_momo_callback` is guest and unauthenticated, as MTN's
callback carries no signature. Its body is never read (D3), so it cannot forge an
outcome. The worst case is extra `reconcile` jobs, bounded by job deduplication,
`MIN_CHECK_INTERVAL` and the per-address limit, which depends on LP-SEC-06.

**Recommendation.** If MTN Cameroon publishes the source addresses of its
callbacks, restrict
`/api/method/local_payments.api.mtn_momo_callback` to them at the reverse proxy.
Add this to the go-live checklist ("Points to confirm before production" in
[`gateways/mtn-momo.md`](gateways/mtn-momo.md)).

### LP-SEC-13. Security headers on the checkout page are not defined

**Severity: Low.** To be checked on the production proxy.

**What happens.** The checkout page takes a phone number and starts a payment.
Neither the app nor the deployment documentation defines
`Strict-Transport-Security`, `Content-Security-Policy` (`frame-ancestors`) or
`X-Frame-Options` for it. Without protection against framing, the "Pay" form can
be embedded in another site (clickjacking).

**Recommendation.** Set HSTS and `frame-ancestors 'self'` (or
`X-Frame-Options: SAMEORIGIN`) at the reverse proxy for the site, and record it
in the installation steps.

---

## Configuration and secrets

### LP-SEC-10. Production settings accept any HTTPS host

**Severity: Low.** Reachable by a System Manager only.

**What happens.** `validate_api_base_url()`
([`mtn_momo_settings.py:171`](../local_payments/local_payments/doctype/mtn_momo_settings/mtn_momo_settings.py#L171))
requires `https://`, and in Sandbox requires the sandbox host. In Production,
any HTTPS host is accepted, including the sandbox host and including a host that
is not MTN's. The merchant credentials are sent there. `target_environment` is
not checked either (`sandbox` is accepted in Production).

**Recommendation.** In Production, refuse `SANDBOX_HOST` and
`target_environment = "sandbox"`. Once MTN Cameroon provides the production host,
validate against an allowlist kept in code, with a site config override for a
host MTN changes.

### LP-SEC-14. Provider access token cached in clear in Redis

**Severity: Info.**

The MTN bearer token is stored by `CacheTokenStore` in the Redis cache for up to
`expires_in − 60` seconds. That is acceptable: anyone who can read Redis already
controls the bench. The deployment must keep Redis bound to localhost or a
private network, with no public exposure.

---

## Transport and TLS certificates

| Link | Direction | Certificate needed on our side? | Current state | Action |
| --- | --- | --- | --- | --- |
| Site → MTN Collection API | Outbound | **No client certificate** in the public contract: the Collection OpenAPI declares only `Ocp-Apim-Subscription-Key`, plus Basic then Bearer auth. MTN's **server** certificate is verified against the `certifi` bundle. | HTTPS enforced by `validate_api_base_url()`. Verification uses `requests` defaults (LP-SEC-11). | Confirm with MTN Cameroon at go-live that production uses no mutual TLS and no outbound IP allowlist. If it does, add `cert=` to the session in `MtnMomoClient` and store the key outside the repo. |
| MTN → site (`X-Callback-Url`) | Inbound | **Yes.** A publicly trusted certificate (for example Let's Encrypt) on the site's public domain. MTN requires HTTPS and a registered callback host in production. A self-signed certificate is not usable. | Not checked (LP-SEC-07). Dev uses an ngrok host. | Valid certificate on the production domain. Register the host in the MTN merchant portal. |
| Payer browser → checkout page and guest endpoints | Inbound | **Yes**, the same site certificate. The token travels in the URL. | Depends on the deployment. | HTTPS only, HTTP redirected, HSTS (LP-SEC-13). |
| Frappe → MariaDB / Redis | Internal | Only if they run on another host over an untrusted network. | Same host in dev. | Use TLS or a private network if they are split out. |

### LP-SEC-07. Callback URL depends on host configuration and is not forced to HTTPS

**Severity: Medium.** Mostly a reliability risk, with a security side.

**What happens.** `callback_url()`
([`mtn_momo_settings.py:100`](../local_payments/local_payments/doctype/mtn_momo_settings/mtn_momo_settings.py#L100))
calls `frappe.utils.get_url()`. That uses `host_name` from the site config when it
is set, and otherwise the **Host header of the payer's request**
(`frappe/utils/data.py`, `get_url`). The scheme then comes from `ssl_certificate`
in the site config, else `http://`. As a result:
- without `host_name`, the callback URL sent to MTN follows whatever `Host` the
  caller sent. On a single-site bench that answers any host, a payer can point
  MTN's callback, which carries their own transaction data, to another host;
- the URL can be `http://`, which MTN refuses in production. The callback is
  then silently lost and the payment is only seen through polling.

`acad.localhost` sets `host_name` to an HTTPS ngrok URL, so this does not show
in dev.

**Recommendation.** When `send_callback` is checked, the Settings `validate()`
should require `host_name` in the site config and, in Production, an `https://`
value. Build the callback URL from `host_name` only, never from the request.

### LP-SEC-11. Outbound TLS relies on library defaults

**Severity: Low.**

**What happens.** `MtnMomoClient` uses a plain `requests.Session()`
([`providers/mtn_momo.py:144`](../local_payments/providers/mtn_momo.py#L144)).
The code never passes `verify=False`, which is good. But:
- the `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` environment variables of the
  worker process change the trusted CA store without any trace in the app;
- no minimum TLS version is set, so the system OpenSSL default applies;
- the `certifi` version is whatever the bench environment has.

**Recommendation.** Pin `verify=certifi.where()` explicitly in the session, so
the environment cannot change it. Mount an `HTTPAdapter` that requires TLS 1.2 or
higher. Keep `certifi` up to date with the bench. Together with LP-SEC-10, this
fixes where credentials go and which certificates are trusted.

---

## Operational improvements

These came out of the 2026-10-05 investigation. They are not vulnerabilities.
LP-IMP-01 to 03 shorten the time a received payment stays unbooked. LP-IMP-04
and 05 make the manager role usable.

### LP-IMP-01. "Retry authorization" button on each session

The scheduler docstring
([`scheduler.py:74`](../local_payments/scheduler.py#L74)) refers to a manual
"Retry authorization" action, and the cancellation message
([`erpnext.py:100`](../local_payments/erpnext.py#L100)) tells the user to "Retry
its authorization from" the session. **That action does not exist:** there is no
form script on `Local Payment` and no whitelisted method around
`reconcile.authorize()`. Today the only way to retry is `bench console`.

**Recommendation: a button on the session form that retries that session only.
Checked as feasible.**

- **Method.** A whitelisted method in the app, for example
  `local_payments.api.retry_authorization(session_name)`, POST only, not guest.
  It checks `frappe.only_for([MANAGER_ROLE, "System Manager"])` and read
  permission on the document, calls `reconcile.authorize(session_name)` and
  returns the new `authorization` state.
- **Button.** `local_payment.js` in the doctype folder, app code per CLAUDE.md,
  never a desk Client Script. It shows "Retry authorization" when
  `status = Paid` and `authorization = Failed`, or `Pending` for longer than
  `PENDING_AUTHORIZATION_GRACE`, then reloads the form.

What was checked:

- **One session only.** `authorize()` works on one session and its reference
  document.
- **Roles without write permission.** `authorize()` saves with
  `ignore_permissions` and commits itself
  ([`reconcile.py:155`](../local_payments/reconcile.py#L155)). The ERPNext
  side already runs through `as_administrator()`.
- **Concurrency with the scheduler.** The `for_update` row lock serializes the
  two runs. The second finds `Done` and does nothing. To avoid a click waiting
  on a lock, take the lock with `wait=False` and answer "an authorization is
  running, try again in a moment", as `void_open_sessions` already does.
- **After the retries are used up.** `authorize()` does not look at
  `authorization_tries`, so a manual retry still works after the scheduler has
  given up. A manual retry does **not** reset the counter: the scheduler stays
  out of it, and a failed manual retry records its error like any other.
- **Effect on accounting.** A retry makes ERPNext create the Payment Entry as
  Administrator. That is acceptable because it only replays a payment MTN
  confirmed, and it is idempotent: a request already `Paid` is skipped.

Tests: the button's method refuses a user without the role, retries a `Failed`
session to `Done`, does nothing on a `Done` session, and answers at once while
the session is locked.

### LP-IMP-02. Alert on the first non-transient authorization failure

Managers are alerted only when `authorization_tries` reaches
`MAX_AUTHORIZATION_TRIES` (5). Retries come after 1 h, 2 h, 4 h and 8 h
([`reconcile.py:94`](../local_payments/reconcile.py#L94)), and the alert job runs
once a day. The first alert therefore comes **15 to 39 hours** after the payment.
A configuration error such as LP-SEC-01 never heals by itself, so those retries
only delay the alert.

**Recommendation.** In `_record_authorization_failure`, separate failures that
need a person (`frappe.ValidationError` and its subclasses, permission errors)
from transient ones (lock timeouts, deadlocks, connection errors). Alert at once
on the first kind (`alert_managers(..., "authorization_failed")`, sent once per
session through a flag column, like the existing alerts). Keep the backoff for
the second kind.

### LP-IMP-03. Payment Requests created with a bad account: prevent, do not repair

**Why fixing the gateway account is not enough.** In ERPNext,
`Payment Request.payment_account` is a `Read Only` field with
`fetch_from: payment_gateway_account.payment_account` and no `allow_on_submit`.
Frappe refreshes `fetch_from` values on a submitted document only for
`allow_on_submit` fields (`frappe/model/base_document.py`, link validation). A
submitted request therefore keeps the account it copied at creation, and its
retries keep failing after the gateway account is fixed. That is what happened
on 2026-10-05.

**Feasibility of an automatic repair.** The only way is a direct write
(`frappe.db.set_value`) into a submitted ERPNext document, bypassing its
controller, on a field ERPNext treats as fixed after submit. `local_payments` can
do it technically without changing ERPNext, but that goes beyond ADR 0002 (the
app calls `set_as_paid()` and writes nothing else into ERPNext documents).

**Decision: prevent rather than repair.**

1. LP-SEC-01 makes `get_payment_url()` refuse a request whose account cannot
   take postings. ERPNext calls it when the request is submitted, so a request
   with a bad account can no longer exist for our gateways.
2. Requests created before that change are repaired by hand, with a documented
   procedure in ARCHITECTURE "Installation and configuration" (or a runbook next
   to it), as done on 2026-10-05:
   1. Fix the `Payment Gateway Account` (Bank or Cash leaf account).
   2. In `bench --site <site> console`, set `payment_account` on each affected
      submitted Payment Request to that account, then `frappe.db.commit()`.
   3. Retry each session with the LP-IMP-01 button, or
      `reconcile.authorize(<session>)` in the console.
3. When an authorization fails because the request's account cannot take
   postings, the LP-IMP-01 button shows a message that points to this
   procedure.

An automatic repair action stays out of scope. If it is wanted later, it needs an
ADR first, and should be restricted to System Manager with an audit comment on
the Payment Request.

### LP-IMP-04. Permissions of Local Payments Manager

What the role should and should not grant, so that it is enough to follow
payments without exposing credentials, site-wide logs or accounting.

| Doctype / action | Today | Plan | Reason |
| --- | --- | --- | --- |
| `Local Payment` | read, report | **Add export** | Reconcile sessions with MTN statements. |
| `Local Payment` write, create, delete | no | **Keep no** | State is written by code only (LP-SEC-15). |
| "Retry authorization" (LP-IMP-01) | does not exist | **Allow**, with System Manager | Handling failed sessions is what the role is for. |
| `Integration Request` | System Manager | **Do not grant** | See below. |
| `Error Log` | System Manager | **Do not grant** | Holds every error of the site. `authorization_error` on the session is enough. |
| `MTN MoMo Settings` | System Manager | **Do not grant** | Holds the merchant credentials. |
| `Payment Request`, `Payment Entry`, `Payment Gateway Account` | ERPNext roles (Accounts User / Manager) | **Do not tie to the role** | If the same person does the accounting, give them the ERPNext role as well. This role must not grant accounting rights. |
| `Notification Log` | All | No change | Each user sees their own alerts. |

Why not open `Integration Request`: a permission covers the whole doctype, so it
would expose every network log on the site (Stripe and others). And as soon as a
Custom DocPerm exists on a doctype, Frappe ignores its standard permissions, so
all of them would have to be copied. If managers need the MTN exchange, show a
summary of it on the `Local Payment` form, as `provider_status` already does.

The export permission ships in `local_payment.json`. The role check for the
retry button lives in its method.

### LP-IMP-05. Local Payments workspace

The app ships no Workspace, so a user who holds only Local Payments Manager has
no entry in the desk and must know the `/app/local-payment` URL. Ship a
"Local Payments" workspace in the module, restricted to Local Payments Manager
and System Manager. It should show shortcuts to `Local Payment`, filtered lists
(`Paid` with `authorization = Failed`, `Unresolved` attempts) and, for System
Manager, the Settings doctypes.

---

## Already in place

These were checked and need no action. They are listed so they don't get flagged
again:

- Secrets are in `Password` fields only, absent from `Exchange` logs and from the
  token request log, and hidden from dataclass `repr`. Traceback variables named
  `token` and `basic_auth_secret` are masked by Frappe's sanitizer.
- The session token comes from `secrets.token_hex` (128 bits), is always
  overwritten in `before_insert`, is checked against a strict pattern, and gives
  the same 404 whether it is malformed or unknown.
- Every guest endpoint has per-token and per-address `rate_limit`, an explicit
  HTTP method, and no access by sequential name.
- Callback bodies and URL parameters are never trusted. Only a merchant-
  authenticated status query moves an attempt (D3).
- Amounts are compared exactly, never rounded, and fractional XAF is refused.
- `Local Payment` is read-only to its roles, and its status fields are written by
  code only.
- The checkout page sends no `Referer` and needs no login.

## References

| Source | Nature | Scope | Used for |
| --- | --- | --- | --- |
| Installed `frappe` v16: `auth.py` (`set_request_ip`), `utils/data.py` (`get_url`), `utils/__init__.py` (`get_traceback`, sanitizer), `__init__.py` (`generate_hash`, `only_for`) | Source code | — | LP-SEC-03, 05, 06, 07, LP-IMP-01 |
| Installed `frappe` v16: `model/document.py` (`validate_set_only_once`), `model/base_document.py` (`fetch_from` on submitted documents), `permissions.py` (`get_doc_permissions`) | Source code | — | LP-SEC-15, LP-IMP-03 |
| Installed `frappe` v16: `utils/user.py` (`get_users_with_role`), `desk/doctype/notification_settings/notification_settings.py` (no email for `Alert`) | Source code | — | LP-SEC-16 |
| Installed `erpnext` v16: `payment_gateway_account.py`, `payment_request.py` (`get_payment_url` on submit, `set_as_paid`, `create_payment_entry`), `payment_request.json` (`payment_account`, `payment_url`), `gl_entry.py` | Source code | — | LP-SEC-01, LP-SEC-17, LP-IMP-03 |
| Installed `payments` (`version-16`, pinned SHA): `utils/utils.py` (`get_checkout_url`) | Source code | — | LP-SEC-02 |
| [`openAPI/collection.json`](openAPI/collection.json), MTN Collection OpenAPI | Official | All countries | Auth schemes (no client certificate) |
| [`gateways/mtn-momo.md`](gateways/mtn-momo.md), "Merchant prerequisites" and MTN "Callback" documentation | Official | All countries | HTTPS and callback host registration in production |
| [Droit Médias Finance, "La loi sur la protection des données à caractère personnel entre en vigueur ce 23 juin 2026"](https://droitmediasfinance.com/index.php/actualites/droit-tech-fintech/1297-cameroun-la-loi-sur-la-protection-des-donnees-a-caractere-personnel-entre-en-vigueur-ce-23-juin-2026) | Press (secondary) | Cameroon | Entry into force of Law No. 2024/017 |
| [CIO Mag, "La protection des données à caractère personnel au Cameroun à la lecture de la loi du 23 décembre 2024"](https://cio-mag.com/?p=59861) | Press (secondary) | Cameroon | Content of Law No. 2024/017, transfers abroad |
| [Digital Business Africa, "L'autorité de protection des données à caractère personnel créée"](https://www.digitalbusiness.africa/cameroun-lautorite-de-protection-des-donnees-a-caractere-personnel-creee/) | Press (secondary) | Cameroon | Data protection authority |
