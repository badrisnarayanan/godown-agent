from PIL import Image, ImageDraw
im = Image.new("RGB", (700, 420), "white"); d = ImageDraw.Draw(im)
lines = ["PURCHASE ORDER", "PO No: ORD-4821", "Customer: Priya Sharma", "Ship to: Madurai, Tamil Nadu",
         "Item: 15in Laptop (LAPTOP-15)", "Qty: 3", "Priority: Express"]
for i, l in enumerate(lines): d.text((40, 40 + i * 48), l, fill="black")
d.rectangle((20, 20, 680, 400), outline="black", width=3)
im.save("samples/order1.png")
