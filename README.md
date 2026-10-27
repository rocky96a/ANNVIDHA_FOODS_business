# ANNVIDHA FOODS — online store and seller hub

A Flask storefront for ANNVIDHA FOODS, featuring Makhana, Sattu and Wood-Pressed Oils, customer accounts, checkout/order management, inventory and staff roles.

## Run locally

```bash
pip install -r requirements.txt
export SECRET_KEY="replace-with-a-long-random-secret"
export ADMIN_USER="owner"
export ADMIN_PASS="replace-with-a-strong-password"
python app.py
```

For production, set `APP_ENV=production` and use `gunicorn app:app`.

- Storefront: `/`
- Customer registration/login/account: `/register`, `/login`, `/account`
- Staff login: `/admin/login`

## Before launch

- Starter product prices, pack sizes, descriptions and stock are examples. Update them in **Staff panel → Products** to match your real products and prices.
- Upload real product photos and verify all ingredient, nutrition, allergen, packaging and shipping details before publishing.
- Add your real business phone, email and contact/address details before launch.
- Do not claim certifications, licences, health benefits, or delivery terms unless verified.
- Set a strong unique `SECRET_KEY` and `ADMIN_PASS`; never deploy using development defaults.
- Set `DB_PATH` to persistent storage and back up the database. Keep `static/uploads` on persistent storage too.

Staff roles: admin, manager, worker. The first admin is created from `ADMIN_USER` / `ADMIN_PASS`; add managers and workers in Admin → Staff.


## Business analytics (added)

- Open **Admin → Business analytics** to filter by start/end date and view order totals, delivered revenue, estimated product costs, gross profit, recorded expenses, net profit, and top products.
- Open **Admin → Expenses** to record business expenses such as packaging, shipping, advertising, wages, rent and utilities.
- Download filtered reports as PDF or CSV.
- Product edit/add forms now include **Cost price** (cost per unit). Enter accurate costs for meaningful profit estimates.

### Financial reporting notes

- Profit is an estimate, not audited accounting.
- Revenue for profit calculations includes delivered orders only.
- Product cost is taken from the current product cost price, so historical profit can be inaccurate if costs changed over time.
- Existing products are migrated with cost price ₹0. Set their real costs in Admin → Products. Until costs are entered, estimated profit will be overstated.
- Add all operating expenses for the selected period. Unrecorded expenses are not included.
- The update creates an `expenses` table and adds a `cost_price` column without deleting existing orders/products/users.
- PDF export requires the added `reportlab` dependency: `pip install -r requirements.txt`.
