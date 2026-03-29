import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO


def generate_pie_chart(by_category: dict[str, float], currency: str, period: str) -> BytesIO:
    if not by_category:
        return None

    labels = [f"{cat} ({amt:.0f})" for cat, amt in by_category.items()]
    sizes = list(by_category.values())
    colors = ["#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
              "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9"]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.pie(sizes, labels=labels, colors=colors[:len(sizes)], autopct="%1.0f%%",
           startangle=90, textprops={"fontsize": 11})
    ax.set_title(f"Расходы за {period} ({currency})", fontsize=14, fontweight="bold")

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def generate_monthly_bars(months_data: list[dict], currency: str) -> BytesIO:
    """months_data: [{"month": "Mar 2026", "total": 52000, "by_category": {...}}, ...]"""
    if not months_data:
        return None

    month_labels = [m["month"] for m in months_data]
    totals = [m["total"] for m in months_data]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(month_labels, totals, color="#4ECDC4", edgecolor="white", linewidth=0.5)

    for bar, total in zip(bars, totals):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(totals) * 0.02,
                f"{total:.0f}", ha="center", va="bottom", fontsize=11, fontweight="bold")

    ax.set_ylabel(currency, fontsize=12)
    ax.set_title("Расходы по месяцам", fontsize=14, fontweight="bold")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf
