import zipfile, sqlite3, os, tempfile, shutil

backup = r'C:\Users\USER\Nutstore\1\LifeUp\LifeupBackup.zip'
tmp = tempfile.mkdtemp()
with zipfile.ZipFile(backup, 'r') as z:
    z.extractall(tmp)
db = os.path.join(tmp, 'databases', 'LifeUpDB.db')
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Verify critical IDs and prices
items_to_check = [
    '📋 凡品回收券', '📋 灵品回收券', 
    '💠 灵蕴碎片', '💠 辉光碎片', '💠 位面晶币',
    '凡品宝箱', '灵品宝箱', '玄品宝箱', '圣品宝箱',
]
for item in items_to_check:
    cur.execute("SELECT id, itemname, price, isdisablepurchase FROM shopitemmodel WHERE itemname LIKE ? AND isdel=0", (f'%{item}%',))
    rows = cur.fetchall()
    for r in rows:
        print(f"  [{r['id']}] {r['itemname']} price={r['price']} disable_purchase={r['isdisablepurchase']}")

# Verify the synth chain numbers
print(f"\n=== 合成链路数值验证 ===")
# N→回收券: 1N = 1券
# 5券 → 1灵蕴碎片: so 1碎片 = 5N cards
# 10灵蕴碎片 → 1灵品宝箱: so 1灵品宝箱 = 50N cards
# 凡品宝箱 costs 100 coin, gives N/R cards → 50*100=5000 coin worth of pulls for 1灵品宝箱
print("凡品链路: 凡品宝箱(100coin)→N卡→5券→1灵蕴碎片→10碎片→灵品宝箱")
print("  → 1灵品宝箱 = 50张N卡 = ~5000coin (凡品宝箱 x50)")
print("  → 灵品宝箱直接购买 = 500coin (回收比直接买贵 10x)")

# R→回收券: 1R = 1券
# 3券 → 1辉光碎片: so 1碎片 = 3R cards
# 50辉光碎片 → 1玄品宝箱: so 1玄品宝箱 = 150R cards
# 灵品宝箱 costs 500 coin → 150*500=75000 coin
print("灵品链路: 灵品宝箱(500coin)→R卡→3券→1辉光碎片→50碎片→玄品宝箱")
print("  → 1玄品宝箱 = 150张R卡 = ~75000coin (灵品宝箱 x150)")

# 1000位面晶币 → 圣品宝箱
# 位面晶币 comes from achievements → pure grind tier
print("圣品链路: 1000位面晶币 → 圣品宝箱")
print("  → 位面晶币仅通过成就获取，无法通过回收产生")

# Count N cards per F-box pull
cur.execute("SELECT COUNT(*) FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'N-%'")
n_count = cur.fetchone()[0]
cur.execute("SELECT COUNT(*) FROM shopitemmodel WHERE isdel=0 AND itemname LIKE 'R-%'") 
r_count = cur.fetchone()[0]
print(f"\n凡品宝箱池: {n_count} N卡 (实际设计有R卡混入凡品池)")
print(f"灵品宝箱池: {r_count} R卡 + SR卡")

conn.close()
shutil.rmtree(tmp)
