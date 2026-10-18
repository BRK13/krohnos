"""Vetoriza a B-refinada (PNG da entrega) com potrace, camada por cor. Uso: tracar.py <png> <pasta-saida>"""
import re, subprocess, sys, tempfile
from pathlib import Path
from PIL import Image
png, out = sys.argv[1], Path(sys.argv[2])
im = Image.open(png).convert("RGB")
K = 6  # amplia antes de traçar: curvas mais lisas

def camada(caixa, tam, teste):
    """Traça os pixels que passam no teste dentro da caixa; devolve os <path> já na escala tam (w, h)."""
    c = im.crop(caixa).resize((tam[0] * K, tam[1] * K), Image.LANCZOS)
    m = Image.new("1", c.size, 1); pm, pc = m.load(), c.load()
    for y in range(c.height):
        for x in range(c.width):
            if teste(*pc[x, y]): pm[x, y] = 0
    with tempfile.TemporaryDirectory() as t:
        m.save(f"{t}/a.pbm"); subprocess.run(["potrace", "-s", "-u", "1", "-t", "20", "-O", "0.4", "--flat", "-o", f"{t}/a.svg", f"{t}/a.pbm"], check=True)
        s = Path(f"{t}/a.svg").read_text()
    tr = re.search(r'<g transform="translate\(([\d.]+),([\d.]+)\) scale\(([\d.]+),(-?[\d.]+)\)"', s)
    ty, sx = float(tr.group(2)), float(tr.group(3))
    ds = " ".join(re.findall(r'<path d="([^"]+)"', s, re.S)).replace("\n", " ")
    # potrace desenha de baixo para cima em unidades de -u; leva para a caixa final (0..w, 0..h)
    return ds, f"matrix({sx / K:g} 0 0 {-sx / K:g} 0 {ty / K:g})"

escuro = lambda r, g, b: r + g + b < 330 and abs(r - b) < 30
areia = lambda r, g, b: r - b > 35 and r + g + b < 640
claro = lambda r, g, b: r + g + b > 560

def svg(vb, corpo, extra=""):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="Pereiraos"><title>Pereiraos</title>\n{extra}{corpo}</svg>\n'

# símbolo principal: caixa medida no PNG (352..784 × 167..681) = 432:514, a mesma proporção do loading
cx = (352, 167, 785, 682)
d1, t1 = camada(cx, (432, 514), escuro); d2, t2 = camada(cx, (432, 514), areia)
(out / "kronos.svg").write_text(svg("0 0 432 514",
    f'<path fill="#171717" transform="{t1}" d="{d1}"/>\n<path fill="#B69A64" transform="{t2}" d="{d2}"/>\n'))

# ícones da prancha (glifo monocromático): claro = carvão sobre marfim; escuro = marfim sobre carvão
for nome, caixa, teste, placa, tinta in (("kronos-icone-claro", (1166, 193, 1312, 380), escuro, "#F2EDE3", "#171717"),
                                         ("kronos-icone-escuro", (1166, 494, 1312, 681), claro, "#171717", "#F2EDE3")):
    w, h = caixa[2] - caixa[0], caixa[3] - caixa[1]
    d, t = camada(caixa, (w, h), teste)
    esc = 360 / h; dx = (512 - w * esc) / 2; dy = (512 - 360) / 2
    (out / f"{nome}.svg").write_text(svg("0 0 512 512",
        f'<g transform="translate({dx:g} {dy:g}) scale({esc:g})"><path fill="{tinta}" transform="{t}" d="{d}"/></g>\n',
        f'<rect width="512" height="512" rx="96" fill="{placa}"/>\n'))
