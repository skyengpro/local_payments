### Local Payments

 Add Orange Money (OM) and Mobile Money (MoMo) gateways on frappe/payments App

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

Requires Frappe v16 and `frappe/payments` on `version-16`, pinned to a known commit.

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app payments --branch version-16
git -C apps/payments checkout cca07d9f9392e2ea0e521c5975151db9e4b6c321
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app local_payments
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/local_payments
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.

### License

mit
