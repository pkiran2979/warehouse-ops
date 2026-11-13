"""Seed synthetic construction-materials data: python seed.py"""
import sys, random, datetime as dt
from app import app, db, Material, Bin, Movement, Document, User, ZONE_RULES
import rag

MATS = [
 ("CEM-001","OPC Cement 50kg","Cement & Binders","bag",400,90),("CEM-002","Sulphate Resistant Cement 50kg","Cement & Binders","bag",200,90),
 ("CEM-003","Tile Adhesive 25kg","Cement & Binders","bag",150,180),
 ("STL-001","Rebar 12mm x 12m","Steel & Rebar","pcs",500,None),("STL-002","Rebar 16mm x 12m","Steel & Rebar","pcs",300,None),
 ("STL-003","Binding Wire 25kg","Steel & Rebar","roll",40,None),("STL-004","Steel Mesh A142","Steel & Rebar","sheet",120,None),
 ("PNT-001","Emulsion Paint White 18L","Paints & Chemicals","drum",60,365),("PNT-002","Primer 18L","Paints & Chemicals","drum",40,365),
 ("PNT-003","Waterproofing Membrane 20L","Paints & Chemicals","drum",30,270),
 ("TIL-001","Porcelain Tile 60x60","Tiles & Finishes","box",300,None),("TIL-002","Ceramic Wall Tile 30x60","Tiles & Finishes","box",250,None),
 ("ELC-001","Cable 2.5mm 100m","Electrical","roll",50,None),("ELC-002","Cable 6mm 100m","Electrical","roll",30,None),
 ("ELC-003","MCB 20A","Electrical","pcs",200,None),
 ("PLM-001","uPVC Pipe 110mm 6m","Plumbing","pcs",150,None),("PLM-002","PPR Pipe 25mm 4m","Plumbing","pcs",200,None),
 ("PLM-003","Gate Valve 1 inch","Plumbing","pcs",80,None),
 ("TMB-001","Plywood 18mm 8x4","Timber","sheet",100,None),("TMB-002","Timber 2x4 4m","Timber","pcs",300,None),
 ("AGG-001","Hollow Block 20cm","Aggregates & Blocks","pcs",3000,None),("AGG-002","Solid Block 10cm","Aggregates & Blocks","pcs",2000,None),
 ("AGG-003","Sand (tonne)","Aggregates & Blocks","t",60,None),("AGG-004","Gravel 20mm (tonne)","Aggregates & Blocks","t",60,None),
]
SOP = """Cement storage SOP. Store cement bags on raised pallets in the dry store, away from walls. Stack no higher than 10 bags.
Use first-in first-out; cement older than 90 days must be tested before use. Keep paint and chemicals in the ventilated chemical store.
Use first-expired first-out for paints and membranes. Rebar is stored in the open yard on timber supports, sorted by diameter.
Receiving procedure: check delivery note against purchase order, inspect for damage, record shortages on the GRN, then putaway to the suggested bin."""

with app.app_context():
    if Material.query.count() and "--reset" not in sys.argv:
        print("Database already has data; skipping seed (use --reset to wipe and reseed).")
        sys.exit(0)
    db.drop_all(); db.create_all()
    for z, n in {"DRY":3,"YARD":4,"CHEM":2,"COVERED":4,"SECURE":2}.items():
        for i in range(1, n+1):
            db.session.add(Bin(code=f"{z}-{i:02d}", zone=z, capacity=5000 if z=="YARD" else 2000))
    for sku, name, cat, unit, mn, sl in MATS:
        db.session.add(Material(sku=sku, name=name, category=cat, unit=unit, min_stock=mn, shelf_life_days=sl))
    for uname, full, role, pw in [("admin", "Demo Admin", "admin", "Admin@1234"), ("store", "Demo Storekeeper", "storekeeper", "Store@1234"),
                                  ("viewer", "Demo Viewer", "viewer", "Viewer@1234")]:
        u = User(username=uname, full_name=full, role=role); u.set_password(pw); db.session.add(u)
    db.session.commit()
    random.seed(7)
    bins = {}
    for b in Bin.query: bins.setdefault(b.zone, []).append(b)
    for m in Material.query:
        for _ in range(random.randint(1, 2)):
            b = random.choice(bins[ZONE_RULES[m.category]])
            qty = round(m.min_stock * random.uniform(0.4, 2.5))
            ts = dt.datetime.utcnow() - dt.timedelta(days=random.randint(2, 60))
            exp = (ts.date() + dt.timedelta(days=m.shelf_life_days)) if m.shelf_life_days else None
            db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=qty, mtype="GRN", ref=f"PO-{random.randint(1000,9999)}", ts=ts, expiry=exp))
            iss = round(qty * random.uniform(0.1, 0.4))
            db.session.add(Movement(material_id=m.id, bin_id=b.id, qty=-iss, mtype="ISSUE", ref=f"SITE-{random.randint(1,5)}",
                                    ts=min(ts + dt.timedelta(days=random.randint(1, 8)), dt.datetime.utcnow() - dt.timedelta(hours=1))))
    d = Document(filename="Warehouse_SOP.txt", stored_as="Warehouse_SOP.txt", doc_type="SOP")
    open("uploads/Warehouse_SOP.txt", "w").write(SOP)
    db.session.add(d); db.session.flush()
    d.chunks = rag.index_document(d.id, d.filename, d.doc_type, SOP)
    db.session.commit()
    print("Demo logins -> admin/Admin@1234, store/Store@1234, viewer/Viewer@1234")
    print("Seeded:", Material.query.count(), "materials,", Bin.query.count(), "bins,", Movement.query.count(), "movements")
