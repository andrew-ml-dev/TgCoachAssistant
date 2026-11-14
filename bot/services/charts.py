"""Графики отчёта: карточка в тёмной теме (хорошо смотрится в обеих темах Telegram)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
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
    kpis: list[tuple[str, str, str]] = field(default_factory=list)  # (подпись, значение, пояснение)


def _style(ax) -> None:
    ax.set_facecolor(PANEL)
    ax.grid(axis="y", color=GRID, lw=1)
    ax.set_axisbelow(True)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(colors=MUTED, length=0, labelsize=10, pad=6)


def _rounded_bars(ax, xs, heights, colors, width, hatches) -> None:
    """Столбики со скруглённым верхом: скругление пересчитано под соотношение осей."""
    fig = ax.figure
    fig.canvas.draw()
    bbox = ax.get_window_extent()
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    aspect = ((y1 - y0) / bbox.height) / ((x1 - x0) / bbox.width)
    r = width * 0.2
    for x, h, c, hatch in zip(xs, heights, colors, hatches):
        if h <= 0:
            continue
        p = FancyBboxPatch((x - width / 2, 0), width, h, boxstyle=f"round,pad=0,rounding_size={r}",
                           mutation_aspect=aspect, facecolor=c, edgecolor=MUTED if hatch else "none",
                           linewidth=0, hatch=hatch, zorder=3)
        ax.add_patch(p)
        # нижние углы делаем прямыми — столбик стоит на оси
        ax.add_patch(plt.Rectangle((x - width / 2, 0), width, min(h, r * aspect), facecolor=c,
                                   edgecolor="none", hatch=hatch, linewidth=0, zorder=3))


def report_chart(d: ReportData) -> bytes:
    plt.rcParams["font.family"] = FONT
    plt.rcParams["hatch.color"] = "#5c5e65"
    plt.rcParams["hatch.linewidth"] = 1.2
    fig = plt.figure(figsize=(8, 9.4), dpi=135, facecolor=BG)
    gs = fig.add_gridspec(3, 1, height_ratios=[0.9, 3, 2.2], hspace=0.32, left=0.08, right=0.86, top=0.93, bottom=0.07)

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
    ax.yaxis.set_major_locator(plt.MaxNLocator(5))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}" if (hi - lo) < 3 else f"{v:.0f}") if wd else plt.NullFormatter())
    ax.tick_params(labelbottom=False)

    # ---------- калории ----------
    ax2 = fig.add_subplot(gs[2], sharex=ax)
    _style(ax2)
    ax2.set_title("Калории за день", loc="left", color=INK, fontsize=12, fontweight="medium", pad=10)
    cd = [x for x in d.days if x in d.kcal]
    if not cd:
        ax2.text(0.5, 0.5, "Записей еды за период нет", transform=ax2.transAxes, color=MUTED, ha="center")
    kmax = max([d.kcal[x][0] for x in cd] + [d.tdee, d.budget])
    ax2.set_ylim(0, kmax * 1.14)
    ax2.set_xlim(xmin, xmax)
    colors, hatches = [], []
    for x in cd:
        k, ok = d.kcal[x]
        if x == d.today:
            colors.append("#4b5563"); hatches.append(None)
        elif not ok:
            colors.append(NODATA); hatches.append("////")
        else:
            colors.append(BLUE if k <= d.budget * 1.05 else OVER); hatches.append(None)
    width = 0.5 if span <= 14 else 0.62
    _rounded_bars(ax2, mdates.date2num(cd), [d.kcal[x][0] for x in cd], colors, width, hatches)
    ax2.axhline(d.tdee, color=INK2, lw=1.1, zorder=4)
    ax2.axhline(d.budget, color=INK, lw=1.3, ls=(0, (3, 3)), zorder=4)
    end = xmax
    ax2.text(end, d.tdee, f"  расход {d.tdee:,.0f}".replace(",", " "), color=INK2, fontsize=9.5, va="center")
    ax2.text(end, d.budget, f"  бюджет {d.budget:,.0f}".replace(",", " "), color=INK, fontsize=9.5, va="center")
    ax2.yaxis.set_major_locator(plt.MaxNLocator(4))
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:,.0f}".replace(",", " ")))
    legend_items = {BLUE: Patch(facecolor=BLUE, label="в бюджете"), OVER: Patch(facecolor=OVER, label="перебор"),
                    NODATA: Patch(facecolor=NODATA, hatch="////", edgecolor="none", label="неполный"),
                    "#4b5563": Patch(facecolor="#4b5563", label="сегодня")}
    handles = [h for c, h in legend_items.items() if c in colors]  # только то, что есть на графике
    if handles:
        ax2.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, -0.3), ncol=len(handles), frameon=False,
                   fontsize=9.5, labelcolor=INK2, handlelength=1.1, handleheight=1.1, columnspacing=1.4)

    # подписи дат
    if span <= 10:
        ax2.set_xticks(d.days)
        ax2.set_xticklabels([f"{WEEKDAYS[x.weekday()]}\n{x.day}" for x in d.days])
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
