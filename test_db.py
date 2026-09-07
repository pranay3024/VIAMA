import psycopg2

DATABASE_URL = "postgresql://postgres.nahcqrxmbzxuixqizdga:Pranay%40pawar3024@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres"

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("SELECT current_database(), current_user, current_schema()")
print("DB/USER/SCHEMA:", cur.fetchone())

cur.execute("SHOW search_path")
print("SEARCH PATH:", cur.fetchone())

cur.execute("""
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_name = 'users'
""")
print("USERS TABLE:", cur.fetchall())

cur.execute("SELECT COUNT(*) FROM public.users")
print("USER COUNT:", cur.fetchone()[0])

cur.close()
conn.close()