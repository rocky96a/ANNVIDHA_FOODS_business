from pathlib import Path
import shutil
import sys

path = Path("app.py")
source = path.read_text()

if "cur.lastrowid" not in source:
    sys.exit("STOP: No lastrowid occurrences found. No changes made.")

if source.count("cur.lastrowid") != 3:
    sys.exit(
        f"STOP: Expected 3 lastrowid occurrences, found "
        f"{source.count('cur.lastrowid')}. No changes made."
    )

backup = Path.home() / "ANNVIDHA_FOODS_pre_postgres_backup" / "app.py.before_generated_ids"
shutil.copy2(path, backup)

# Order creation: use RETURNING id on PostgreSQL.
old_order = '''    cur = con.execute("INSERT INTO orders(name,phone,address,pincode,items,total,created_at,user_id) VALUES(?,?,?,?,?,?,?,?)",
        (name, phone, addr, pin, json.dumps(items), total, now().isoformat(timespec="seconds"), u["id"]))
    con.execute("UPDATE users SET address=?,pincode=? WHERE id=? AND address=''", (addr, pin, u["id"]))
    con.commit()
    return jsonify(ok=True, order_id=cur.lastrowid, total=total)
'''

new_order = '''    order_sql = "INSERT INTO orders(name,phone,address,pincode,items,total,created_at,user_id) VALUES(?,?,?,?,?,?,?,?)"
    if USE_POSTGRES:
        order_sql += " RETURNING id"
    cur = con.execute(order_sql,
        (name, phone, addr, pin, json.dumps(items), total, now().isoformat(timespec="seconds"), u["id"]))
    order_id = cur.fetchone()["id"] if USE_POSTGRES else cur.lastrowid
    con.execute("UPDATE users SET address=?,pincode=? WHERE id=? AND address=''", (addr, pin, u["id"]))
    con.commit()
    return jsonify(ok=True, order_id=order_id, total=total)
'''

if old_order not in source:
    sys.exit("STOP: Order insert block did not match. No app.py changes made.")

source = source.replace(old_order, new_order, 1)

# Customer registration: obtain the inserted user ID.
old_user = '''            cur = db().execute("INSERT INTO users(name,phone,email,password_hash) VALUES(?,?,?,?)", (name, phone, f.get("email", "").strip(), generate_password_hash(pw)))
            db().commit(); session.pop("uid", None); session["uid"] = cur.lastrowid
'''

new_user = '''            user_sql = "INSERT INTO users(name,phone,email,password_hash) VALUES(?,?,?,?)"
            if USE_POSTGRES:
                user_sql += " RETURNING id"
            cur = db().execute(user_sql, (name, phone, f.get("email", "").strip(), generate_password_hash(pw)))
            user_id = cur.fetchone()["id"] if USE_POSTGRES else cur.lastrowid
            db().commit(); session.pop("uid", None); session["uid"] = user_id
'''

if old_user not in source:
    sys.exit("STOP: User insert block did not match. No app.py changes made.")

source = source.replace(old_user, new_user, 1)

# Product creation: obtain the ID before restoring stock.
old_product = '''            cur = con.execute("INSERT INTO products(name,description,price,mrp,image_url,active,cost_price) VALUES(?,?,?,?,?,?,?)", vals)
            qty = int(f.get("stock") or 0)
            if qty: restore(cur.lastrowid, qty)
'''

new_product = '''            product_sql = "INSERT INTO products(name,description,price,mrp,image_url,active,cost_price) VALUES(?,?,?,?,?,?,?)"
            if USE_POSTGRES:
                product_sql += " RETURNING id"
            cur = con.execute(product_sql, vals)
            product_id = cur.fetchone()["id"] if USE_POSTGRES else cur.lastrowid
            qty = int(f.get("stock") or 0)
            if qty: restore(product_id, qty)
'''

if old_product not in source:
    sys.exit("STOP: Product insert block did not match. No app.py changes made.")

source = source.replace(old_product, new_product, 1)

path.write_text(source)
print("Generated-ID compatibility patch applied.")
print("Backup:", backup)
