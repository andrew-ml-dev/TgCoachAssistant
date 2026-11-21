"""Графики отчёта: карточка в тёмной теме (хорошо смотрится в обеих темах Telegram)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.transforms  # noqa: E402,F401
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Patch  # noqa: E402

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
for _f in FONT_DIR.glob("*.ttf"):
    font_manager.fontManager.addfont(str(_f))
FONT = "Inter" if any(FONT_DIR.glob("Inter-*.ttf")) else "DejaVu Sans"

# Палитра: тёмная поверхность, категориальные слоты 1–2 (проверены валидатором на CVD и контраст)
BG = "#17181b"
PANEL = "#1f2125"
INK = "#f0efec"
INK2 = "#c3c2b7"
MUTED = "#8a8984"
GRID = "#2c2e33"
BLUE = "#3987e5"      # вес / в бюджете
ORANGE = "#d95926"    # расчётный вес
OVER = "#ec835a"      # перебор
NODATA = "#3a3c42"    # неполный день
GREEN = "#2f9e6e"     # дефицит
GREEN_LIGHT = "#7fd1a8"  # часть дефицита от тренировки
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


@dataclass
class ReportData:
    title: str
    subtitle: str
    days: list[date]
    weights: dict[date, tuple[float, float]]            # дата -> (вес, тренд)
    expected: list[tuple[date, float]]                  # расчётный вес по дефициту
    kcal: dict[date, tuple[float, bool]]                # дата -> (ккал, день надёжный)
    budget: float
    tdee: float
    target: float
    today: date
    kpis: list[tuple[str, str, str]] = field(default_factory=list)
    # (подпись, значение, пояснение) у kpis; ниже — по дням, если план или тренировки меняли цифры
    target_deficit: float = 0                    # дефицит под выбранный темп — линия цели
    tdees: dict = field(default_factory=dict)    # дата → расход, действовавший в тот день
    workouts: dict[date, float] = field(default_factory=dict)       # дата -> учтённые ккал тренировки


def week_runs(d: ReportData) -> list[dict]:
    """Недели с понедельника: накопленный дефицит и цель на конец каждого прошедшего дня.

    Дефицит дня = расход того дня + тренировка − съедено. Неполный или пустой день не считается ни в дефицит,
    ни в цель (иначе забытый ужин выглядел бы как огромный дефицит). Сегодня и дальше — только цель (proj).
    pts — (граница дня по оси, дефицит, цель); left — сколько дней недели осталось, включая сегодня.
    """
    runs: list[dict] = []
    for x in d.days:
        if not runs or x.weekday() == 0:
            runs.append({"pts": [(mdates.date2num(x) - 0.5, 0.0, 0.0)], "proj": [], "daily": {}, "skipped": [],
                         "cum": 0.0, "tgt": 0.0, "days": 0, "left": 0, "end": x})
        r = runs[-1]
        r["end"] = x
        edge = mdates.date2num(x) + 0.5
        if x >= d.today:
            last = r["proj"][-1][1] if r["proj"] else r["tgt"]
            r["proj"].append((edge, last + d.target_deficit))
            r["left"] += 1
            continue
        if x in d.kcal and d.kcal[x][1]:
            v = d.tdees.get(x, d.tdee) + d.workouts.get(x, 0) - d.kcal[x][0]
            r["daily"][x] = v
            r["cum"] += v
            r["tgt"] += d.target_deficit
            r["days"] += 1
        else:
            r["skipped"].append(x)
        r["pts"].append((edge, r["cum"], r["tgt"]))
    return runs


def _style(ax) -> None:
    ax.set_facecolor(PANEL)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, length=0, labelsize=10, pad=6)


def report_chart(d: ReportData) -> bytes:
    plt.rcParams["font.family"] = FONT
    plt.rcParams["hatch.color"] = "#5c5e65"
    plt.rcParams["hatch.linewidth"] = 1.2
    fig = plt.figure(figsize=(8, 9.4), dpi=135, facecolor=BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[0.9, 3, 2.2], hspace=0.32, left=0.08, right=0.86, top=0.93, bottom=0.09)

    # ---------- шапка ----------
    fig.text(0.08, 0.965, d.title, color=INK, fontsize=17, fontweight="semibold", va="center")
    fig.text(0.08, 0.94, d.subtitle, color=MUTED, fontsize=10.5, va="center")
    head = fig.add_subplot(gs[0])
    head.axis("off")
    n = max(1, len(d.kpis))
    for i, (label, value, note) in enumerate(d.kpis):
        x = i / n
        head.add_patch(FancyBboxPatch((x + 0.005, 0.02), 1 / n - 0.03, 0.96, transform=head.transAxes,
                                      boxstyle="round,pad=0,rounding_size=0.06", mutation_aspect=0.3,
                                      facecolor=PANEL, edgecolor="none"))
        head.text(x + 0.045, 0.76, label, transform=head.transAxes, color=MUTED, fontsize=9.5, va="center")
        head.text(x + 0.045, 0.43, value, transform=head.transAxes, color=INK, fontsize=19,
                  fontweight="semibold", va="center")
        head.text(x + 0.045, 0.15, note, transform=head.transAxes, color=INK2, fontsize=9, va="center")

    span = (d.days[-1] - d.days[0]).days + 1
    # даты как числа: date − timedelta(0.6) отбросил бы дробную часть
    xmin, xmax = mdates.date2num(d.days[0]) - 0.6, mdates.date2num(d.days[-1]) + 0.6

    # ---------- вес ----------
    ax = fig.add_subplot(gs[1])
    _style(ax)
    ax.set_title("Вес, кг", loc="left", color=INK, fontsize=12, fontweight="medium", pad=10)
    wd = sorted(d.weights)
    if wd:
        w = [d.weights[x][0] for x in wd]
        t = [d.weights[x][1] for x in wd]
        ev = [v for _, v in d.expected]
        lo = min(w + t + ev) - 0.4
        hi = max(w + t + ev) + 0.4
        if lo - 1.5 < d.target < hi:
            lo = min(lo, d.target - 0.3)
        ax.set_ylim(lo, hi)
        ax.scatter(wd, w, s=22, color="#6d6e73", zorder=3, linewidths=0, label="на весах")
        if len(wd) >= 2:
            # мягкая заливка под трендом
            xs = mdates.date2num(wd)
            poly = ax.fill_between(xs, t, lo, color="none", linewidth=0)
            grad = np.linspace(0.28, 0.0, 256).reshape(-1, 1)
            rgba = np.zeros((256, 1, 4))
            rgba[..., :3] = matplotlib.colors.to_rgb(BLUE)
            rgba[..., 3] = grad
            img = ax.imshow(rgba, aspect="auto", extent=(xs[0], xs[-1], lo, max(t)), origin="upper", zorder=1)
            img.set_clip_path(poly.get_paths()[0], transform=ax.transData)
            ax.plot(wd, t, color=BLUE, lw=2.8, zorder=4, solid_capstyle="round", label="тренд")
        ax.annotate(f"{t[-1]:.1f}", (wd[-1], t[-1]), xytext=(10, 0), textcoords="offset points",
                    color=BLUE, fontsize=12, fontweight="semibold", va="center", annotation_clip=False)
        if len(d.expected) >= 2:
            ex = [x for x, _ in d.expected]
            ax.plot(ex, ev, color=ORANGE, lw=2.2, ls=(0, (4, 3)), zorder=4, dash_capstyle="round",
                    label="расчётный по дефициту")
            gap = abs(ev[-1] - t[-1])
            ax.annotate(f"{ev[-1]:.1f}", (ex[-1], ev[-1]), xytext=(10, -14 if ev[-1] < t[-1] and gap < 0.25 else 0),
                        textcoords="offset points", color=ORANGE, fontsize=11, fontweight="medium",
                        va="center", annotation_clip=False)
        if lo < d.target < hi:
            ax.axhline(d.target, color="#16a34a", lw=1.2, ls=(0, (1, 3)), zorder=2)
            ax.text(xmin, d.target, f" цель {d.target:g}", color="#4ade80", fontsize=9, va="bottom")
        leg = ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=3, frameon=False, fontsize=9.5,
                        labelcolor=INK2, handlelength=1.6, columnspacing=1.2, borderaxespad=0.3)
        for h in leg.legend_handles:
            h.set_alpha(1)
    else:
        ax.text(0.5, 0.5, "Взвешиваний пока нет", transform=ax.transAxes, color=MUTED, ha="center")
    ax.set_xlim(xmin, xmax)
    # целые кг — только при размахе от 3 кг, и тогда деления тоже целые: иначе «90, 90» на соседних делениях
    ax.yaxis.set_major_locator(plt.MaxNLocator(5, integer=bool(wd) and (hi - lo) >= 3))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}" if (hi - lo) < 3 else f"{v:.0f}") if wd else plt.NullFormatter())
    ax.tick_params(labelbottom=False)

    # ---------- дефицит нарастающим с понедельника ----------
    ax2 = fig.add_subplot(gs[2], sharex=ax)
    _style(ax2)
    runs = week_runs(d)
    head = "Дефицит с понедельника, ккал"
    if d.target_deficit:
        head += f"  ·  цель {d.target_deficit:,.0f} в день".replace(",", " ")
    ax2.set_title(head, loc="left", color=INK, fontsize=12, fontweight="medium", pad=10)
    ax2.set_xlim(xmin, xmax)
    used = set()
    vals = [0.0]
    for r in runs:
        bx = [p[0] for p in r["pts"]]
        act = [p[1] for p in r["pts"]]
        tg = [p[2] for p in r["pts"]]
        vals += act + tg + [p[1] for p in r["proj"]]
        for x in r["skipped"]:
            xn = mdates.date2num(x)
            ax2.axvspan(xn - 0.5, xn + 0.5, facecolor=NODATA, hatch="////", edgecolor="none", alpha=0.6, zorder=1)
            used.add("nodata")
        if r["proj"]:  # цель на оставшиеся дни недели
            px = [bx[-1]] + [p[0] for p in r["proj"]]
            py = [tg[-1]] + [p[1] for p in r["proj"]]
            ax2.plot(px, py, color=MUTED, lw=1.2, ls=(0, (3, 3)), zorder=3)
            ax2.annotate(f"{py[-1]:,.0f}".replace(",", " "), (px[-1], py[-1]), xytext=(0, 5),
                         textcoords="offset points", ha="right", va="bottom", color=MUTED, fontsize=8.5)
        if len(bx) < 2:
            continue
        ax2.plot(bx, tg, color=INK, lw=1.3, ls=(0, (3, 3)), zorder=4)
        ax2.fill_between(bx, act, tg, where=[a >= t for a, t in zip(act, tg)], interpolate=True,
                         color=GREEN, alpha=0.22, linewidth=0, zorder=2)
        ax2.fill_between(bx, act, tg, where=[a < t for a, t in zip(act, tg)], interpolate=True,
                         color=OVER, alpha=0.25, linewidth=0, zorder=2)
        ax2.plot(bx, act, color=GREEN_LIGHT, lw=2.6, zorder=5, solid_capstyle="round", marker="o",
                 markersize=3.5, markevery=list(range(1, len(bx))))
        used.add("line")
        gap = act[-1] - tg[-1]
        used.add("ahead" if gap >= 0 else "behind")
        gtxt = f"{abs(gap):,.0f}".replace(",", " ")
        gtxt = f"+{gtxt} впереди" if gap >= 0 else f"−{gtxt} отстаю"
        if r is runs[-1]:
            ax2.annotate(f"{act[-1]:,.0f}".replace(",", " ").replace("-", "−"), (bx[-1], act[-1]),
                         xytext=(6, 7), textcoords="offset points", color=GREEN_LIGHT, fontsize=11,
                         fontweight="semibold", va="bottom", annotation_clip=False)
            ax2.annotate(gtxt, (bx[-1], act[-1]), xytext=(6, -5), textcoords="offset points",
                         color=GREEN_LIGHT if gap >= 0 else OVER, fontsize=9, va="top", annotation_clip=False)
        else:
            ax2.annotate(gtxt.split()[0], (bx[-1], max(act[-1], tg[-1])), xytext=(-2, 4), textcoords="offset points",
                         ha="right", va="bottom", color=GREEN_LIGHT if gap >= 0 else OVER, fontsize=8)
        if span > 14:
            ax2.axvline(bx[0], color=GRID, lw=1, zorder=1)
    if not any(len(r["pts"]) > 1 for r in runs):
        ax2.text(0.5, 0.55, "Полных дней на этой неделе пока нет", transform=ax2.transAxes, color=MUTED,
                 ha="center")
    top, bottom = max(vals), min(vals)
    pad = (top - bottom) * 0.14 + 50
    ax2.set_ylim(bottom - (pad if bottom < 0 else pad * 0.25), top + pad)
    ax2.axhline(0, color=MUTED, lw=1, zorder=2)
    ax2.yaxis.set_major_locator(plt.MaxNLocator(4))
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}".replace(",", " ").replace("-", "−")))
    legend = {"line": plt.Line2D([], [], color=GREEN_LIGHT, lw=2.6, label="дефицит"),
              "target": plt.Line2D([], [], color=INK, lw=1.3, ls=(0, (3, 3)), label="цель"),
              "ahead": Patch(facecolor=GREEN, alpha=0.4, label="впереди"),
              "behind": Patch(facecolor=OVER, alpha=0.45, label="отстаю"),
              "nodata": Patch(facecolor=NODATA, hatch="////", edgecolor="none", label="неполный, не считается")}
    if "line" in used:
        used.add("target")
    handles = [h for k, h in legend.items() if k in used]
    if handles:
        ax2.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, -0.38), ncol=len(handles), frameon=False,
                   fontsize=9.5, labelcolor=INK2, handlelength=1.4, handleheight=1.1, columnspacing=1.2)

    # подписи дат
    if span <= 10:
        ax2.set_xticks(d.days)
        # под датой — дефицит самого дня
        daily = {k: v for r in runs for k, v in r["daily"].items()}

        def _day(x):
            if x in daily:
                return f"{daily[x]:+,.0f}".replace(",", " ").replace("-", "−")
            return "идёт" if x == d.today else ("—" if x < d.today else "")
        ax2.set_xticklabels([f"{WEEKDAYS[x.weekday()]} {x.day}\n{_day(x)}" for x in d.days])
    else:
        ax2.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    for lbl in ax2.get_xticklabels():
        lbl.set_color(MUTED)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def weight_chart(rows, target: float) -> bytes:
    """Совместимость: простой график веса (используется, если данных о еде нет)."""
    days = [date.fromisoformat(r["date"]) for r in rows]
    data = ReportData(title="Вес", subtitle="", days=days,
                      weights={date.fromisoformat(r["date"]): (r["weight"], r["trend"]) for r in rows},
                      expected=[], kcal={}, budget=0, tdee=0, target=target, today=days[-1])
    return report_chart(data)
