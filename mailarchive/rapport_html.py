"""Rapports HTML autonomes (un seul fichier, sans connexion internet), clair et sombre."""
from __future__ import annotations

import html
from dataclasses import dataclass, field


def taille_lisible(n: float) -> str:
    for unite in ("octets", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unite == "To":
            if unite == "octets":
                return f"{int(n)} octets"
            return f"{n:.1f} {unite}".replace(".", ",")
        n /= 1024
    return str(n)


def nombre(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def pluriel(n: int, mot: str, mots: str | None = None) -> str:
    return f"{nombre(n)} {mot if n <= 1 else (mots or mot + 's')}"


@dataclass
class Tuile:
    valeur: str
    libelle: str
    detail: str = ""


@dataclass
class Barres:
    """Barres horizontales : une seule série (octets), libellé et valeurs écrits en clair."""
    titre: str
    lignes: list[tuple[str, int, int]]  # (libellé, octets, nombre de fichiers)
    note: str = ""


@dataclass
class Tableau:
    titre: str
    entetes: list[str]
    lignes: list[list[str]]
    note: str = ""
    numeriques: set[int] = field(default_factory=set)  # colonnes alignées à droite


CSS = """
.viz-root{color-scheme:light;--surface-0:#f4f3f0;--surface-1:#fcfcfb;--border:#e2e1dc;--grid:#ecebe7;
--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#77766f;--series-1:#2a78d6;--track:#ecebe7;
--warn-bg:#fff6e0;--warn-border:#fab219}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--surface-0:#121211;--surface-1:#1a1a19;--border:#33332f;--grid:#2a2a27;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--text-muted:#9a998f;--series-1:#3987e5;--track:#262624;--warn-bg:#2b2412;--warn-border:#fab219}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-0:#121211;--surface-1:#1a1a19;--border:#33332f;
--grid:#2a2a27;--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#9a998f;--series-1:#3987e5;
--track:#262624;--warn-bg:#2b2412;--warn-border:#fab219}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-0);color:var(--text-primary);
font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
.viz-root{background:var(--surface-0);min-height:100vh;padding:32px 20px 64px}
main{max-width:980px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--text-secondary);margin:0 0 24px}
h2{font-size:17px;margin:0 0 12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:20px 22px;margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:0 0 18px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
.tile .v{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .l{color:var(--text-secondary);font-size:13.5px;margin-top:2px}
.tile .d{color:var(--text-muted);font-size:12.5px;margin-top:6px}
.tile.hero{border-color:var(--series-1)}
.note{color:var(--text-muted);font-size:13px;margin:10px 0 0}
.warn{background:var(--warn-bg);border:1px solid var(--warn-border);border-radius:10px;padding:10px 14px;margin:0 0 18px}
.bars{display:grid;grid-template-columns:minmax(120px,240px) 1fr auto;gap:6px 14px;align-items:center}
.bars .lab{color:var(--text-secondary);font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bars .trk{height:14px;background:transparent;position:relative}
.bars .bar{height:14px;background:var(--series-1);border-radius:0 4px 4px 0;min-width:2px}
.bars .val{font-variant-numeric:tabular-nums;font-size:13.5px;white-space:nowrap;text-align:right}
.bars .val span{color:var(--text-muted)}
.row{display:contents}
.row:hover .lab,.row:hover .val{color:var(--text-primary)}
.row:hover .bar{filter:brightness(1.12)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{color:var(--text-muted);font-weight:600;text-align:left;border-bottom:1px solid var(--border);padding:6px 8px}
td{border-bottom:1px solid var(--grid);padding:6px 8px;vertical-align:top}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:hover td{background:var(--grid)}
details summary{cursor:pointer;color:var(--text-secondary);font-size:13.5px;margin-top:10px}
.toggle{float:right;background:var(--surface-1);color:var(--text-secondary);border:1px solid var(--border);
border-radius:8px;padding:4px 10px;font:inherit;font-size:13px;cursor:pointer}
@media print{.toggle{display:none}}
"""

JS = """
document.getElementById('theme').addEventListener('click',()=>{const r=document.documentElement;
const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;
r.dataset.theme=dark?'light':'dark';});
"""


def _e(s) -> str:
    return html.escape(str(s))


def _barres(b: Barres) -> str:
    if not b.lignes:
        return ""
    maxi = max(o for _, o, _ in b.lignes) or 1
    rows = []
    table = []
    for lab, octets, n in b.lignes:
        pct = 100 * octets / maxi
        info = f"{lab} : {taille_lisible(octets)} — {pluriel(n, 'fichier')}"
        rows.append(
            f'<div class="row" title="{_e(info)}"><div class="lab">{_e(lab)}</div>'
            f'<div class="trk"><div class="bar" style="width:{pct:.2f}%"></div></div>'
            f'<div class="val">{_e(taille_lisible(octets))} <span>· {_e(nombre(n))}</span></div></div>'
        )
        table.append(f"<tr><td>{_e(lab)}</td><td class='n'>{_e(taille_lisible(octets))}</td><td class='n'>{_e(nombre(n))}</td></tr>")
    note = f'<p class="note">{_e(b.note)}</p>' if b.note else ""
    return (
        f'<section class="card"><h2>{_e(b.titre)}</h2><div class="bars">{"".join(rows)}</div>{note}'
        f'<details><summary>Voir sous forme de tableau</summary><table><tr><th></th><th class="n">Volume</th>'
        f'<th class="n">Fichiers</th></tr>{"".join(table)}</table></details></section>'
    )


def _tableau(t: Tableau) -> str:
    if not t.lignes:
        return ""
    th = "".join(f'<th class="{"n" if i in t.numeriques else ""}">{_e(h)}</th>' for i, h in enumerate(t.entetes))
    trs = "".join(
        "<tr>" + "".join(f'<td class="{"n" if i in t.numeriques else ""}">{_e(c)}</td>' for i, c in enumerate(l)) + "</tr>"
        for l in t.lignes
    )
    note = f'<p class="note">{_e(t.note)}</p>' if t.note else ""
    return f'<section class="card"><h2>{_e(t.titre)}</h2><table><tr>{th}</tr>{trs}</table>{note}</section>'


def page(titre: str, sous_titre: str, tuiles: list[Tuile], blocs: list, alertes: list[str] | None = None) -> str:
    t = "".join(
        f'<div class="tile{" hero" if i == 0 else ""}"><div class="v">{_e(x.valeur)}</div>'
        f'<div class="l">{_e(x.libelle)}</div>{f"<div class=d>{_e(x.detail)}</div>" if x.detail else ""}</div>'
        for i, x in enumerate(tuiles)
    )
    a = "".join(f'<div class="warn">⚠ {_e(x)}</div>' for x in (alertes or []))
    corps = "".join(_barres(b) if isinstance(b, Barres) else _tableau(b) for b in blocs)
    return (
        f'<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" '
        f'content="width=device-width,initial-scale=1"><title>{_e(titre)}</title><style>{CSS}</style></head>'
        f'<body><div class="viz-root"><main><button class="toggle" id="theme">Clair / sombre</button>'
        f'<h1>{_e(titre)}</h1><p class="sub">{_e(sous_titre)}</p>{a}<div class="tiles">{t}</div>{corps}'
        f"</main></div><script>{JS}</script></body></html>"
    )
