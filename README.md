Sales Data Analysis System
Flask + Pandas/NumPy/Matplotlib + MySQL backend, Bootstrap/vanilla-JS frontend (served by the backend).

Run
cd backend
python -m venv venv && venv\Scripts\activate      # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
python app.py          # a ready-to-use backend/.env is included (DB_ENGINE=auto)                                    # open http://127.0.0.1:5000
The database sales_analysis and its tables are created automatically on first start (MySQL must be running). If Windows blocks activation: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass.

MySQL password - where to get it
It is the root password you chose when you installed MySQL (MySQL Installer -> "Accounts and Roles"); it is not stored anywhere you can look it up. Put it in backend/.env as DB_PASSWORD=.... Test it with mysql -u root -p or MySQL Workbench (the saved "Local instance" connection). Forgot it? Reset it (MySQL Installer -> Reconfigure, or the MySQL docs "Resetting the root password"), or ignore MySQL: with the default DB_ENGINE=auto the app prints the problem and falls back to SQLite automatically. Use DB_ENGINE=mysql to force MySQL only. Try it: Import CSV tab -> upload backend/sample_data/sales_sample.csv (189 rows: 180 good, 2 duplicates, 7 malformed).

Tests
cd backend && python -m unittest discover -s tests -v (temporary SQLite DB; covers revenue maths, malformed rows, duplicate policy, CRUD, filters, charts).

Duplicate-import policy
A sale's identity is product name + sale date + quantity + unit price + region (case-insensitive), stored as a SHA-1 row_hash with a UNIQUE index. A row matching an earlier row in the same file or any earlier import is skipped and reported (duplicate_in_file / duplicate_existing); the rest of the file is still imported. Manually added sales are exempt (genuine repeat sales are allowed). Deleting a sale lets it be re-imported. Existing product category wins over a CSV's category.

Validation
Whole file rejected if: not UTF-8, empty, or a required column is missing. Per row (skipped + reported with line number and reason): wrong column count, empty product/category/region, invalid or future date (YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY), quantity not a whole number > 0, unit price not a number or negative. Full report downloadable as CSV.

Revenue accuracy
Revenue = quantity x unit price computed in integer paise/cents with NumPy (no float drift). Monthly growth % = (month - previous) / previous; shown as "–" when the previous month had no revenue.

API (JSON)
POST /api/import · GET /api/imports · GET /api/imports/<id>/report.csv · GET|POST /api/products · DELETE /api/products/<id> · GET|POST /api/sales · DELETE /api/sales/<id> GET /api/analytics/{summary,products,monthly,categories,regions} and GET /api/charts/{monthly,products}.png (filters: start,end,category,region).
