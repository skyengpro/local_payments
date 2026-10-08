# Local Payments

Mobile money gateways for Cameroon, built on [`frappe/payments`](https://github.com/frappe/payments).

The payer gets a link to a payment page, enters their number, and approves the request on their phone.
The site confirms the payment by asking the provider itself, then tells the document that asked for it. On an ERPNext site, the Payment Request is marked paid and ERPNext books the Payment Entry.

Any document that already accepts a `frappe/payments` gateway can use these gateways unchanged: ERPNext Payment Requests, LMS, or your own doctypes.

## Status

| Gateway                             | State                                                             |
| ----------------------------------- | ----------------------------------------------------------------- |
| MTN MoMo (Collection, RequestToPay) | Implemented and tested against the MTN sandbox                    |
| Orange Money (Local/USSD)           | Waiting for the merchant contract and its technical documentation |

Refunds, disbursements and recurring payments are out of scope.

## Requirements

- Frappe v16 with a working scheduler and Redis.
- `frappe/payments`, branch `version-16`, at commit `cca07d9f9392e2ea0e521c5975151db9e4b6c321`. It
  publishes no versions, so the app is tested against that commit only.
- ERPNext v16 is optional. Without it, the gateways work for every other consumer.
- An MTN MoMo merchant account for the Collection product: subscription key, API user and API key.
  MTN provides the production base URL and target environment at go-live.
- For production, a public domain served over HTTPS with a certificate from a public authority. MTN
  only calls back `https://` addresses.

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app payments --branch version-16
git -C apps/payments checkout cca07d9f9392e2ea0e521c5975151db9e4b6c321
bench get-app https://github.com/skyengpro/local_payments.git
bench --site <site> install-app payments local_payments
```

## Configuration

1. In the site config, set `host_name` to the site's public `https://` address, and keep
   `developer_mode` off in production. Frappe v16 caches the site config for up to a minute.

   ```bash
   bench --site <site> set-config host_name https://pay.example.com
   ```
2. Give the `Local Payments Manager` role to at least one person. Payment problems that
   need someone (a duplicate payment, an amount mismatch, a payment that could not be booked) are sent
   to that role as desk notifications. If nobody holds it, they go to System Managers.
3. Create one `MTN MoMo Settings` document per merchant contract. Saving it creates the
   `MTN MoMo-<name>` payment gateway. Every field is described in
   [docs/gateways/mtn-momo.md](docs/gateways/mtn-momo.md#configuration).
4. On an ERPNext site, when a gateway is enabled, ERPNext creates a `Payment Gateway Account` for the default
   company. Check that it points to a Bank or Cash ledger account in the gateway's currency, and create
   one for every other company that takes payments.
5. In production, register the site's host as the callback host in the MTN merchant
   portal.

## Documentation

- [Architecture](docs/ARCHITECTURE.md): components, payment lifecycle, security, design decisions.
- [MTN MoMo](docs/gateways/mtn-momo.md) and [Orange Money](docs/gateways/orange-money.md): provider
  contracts and settings.
- [Decision records](docs/decisions/).

## Contributing

The app uses [pre-commit](https://pre-commit.com/#installation) (ruff, eslint, prettier):

```bash
cd apps/local_payments
pre-commit install
```

Tests run on a site with the app installed. Use one site without ERPNext and one with it, as CI does.

```bash
bench --site <site> run-tests --app local_payments
```

## License

MIT
