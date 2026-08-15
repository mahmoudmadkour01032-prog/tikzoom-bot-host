"""Tier (speed level) system: 5 tiers with different point thresholds and file caps."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    level: int
    label_ar: str
    label_en: str
    required_points: int
    max_files_normal: int
    max_files_vip: int
    description_ar: str
    description_en: str


# Tier table per user spec:
#   Tier 1: free (0 points)  → 1 file
#   Tier 2: 5 points          → 1 file (faster)
#   Tier 3: 10 points         → 1 file (faster still)
#   Tier 4: 20 points         → 1 file (highest speed for normal users)
#   Tier 5: VIP only          → 20 files (unlimited speed; VIP/admin get 5 files per tier instead)
TIERS: tuple[Tier, ...] = (
    Tier(
        level=1,
        label_ar="🐢 سرعة 1 — مجانية",
        label_en="Tier 1 — Free",
        required_points=0,
        max_files_normal=1,
        max_files_vip=5,
        description_ar="السرعة العادية المجانية. ملف واحد لكل مستخدم.",
        description_en="Free tier. 1 file per regular user.",
    ),
    Tier(
        level=2,
        label_ar="🚶 سرعة 2 — 5 نقاط",
        label_en="Tier 2 — 5 points",
        required_points=5,
        max_files_normal=1,
        max_files_vip=5,
        description_ar="ادعُ 5 أصدقاء (5 نقاط) لتفعيل سرعة 2.",
        description_en="Invite 5 friends (5 points) to unlock Tier 2.",
    ),
    Tier(
        level=3,
        label_ar="🏃 سرعة 3 — 10 نقاط",
        label_en="Tier 3 — 10 points",
        required_points=10,
        max_files_normal=1,
        max_files_vip=5,
        description_ar="ادعُ 10 أصدقاء (10 نقاط) لتفعيل سرعة 3.",
        description_en="Invite 10 friends (10 points) to unlock Tier 3.",
    ),
    Tier(
        level=4,
        label_ar="🏎️ سرعة 4 — 20 نقطة",
        label_en="Tier 4 — 20 points",
        required_points=20,
        max_files_normal=1,
        max_files_vip=5,
        description_ar="ادعُ 20 صديقًا (20 نقطة) لتفعيل سرعة 4.",
        description_en="Invite 20 friends (20 points) to unlock Tier 4.",
    ),
    Tier(
        level=5,
        label_ar="🚀 سرعة 5 — VIP",
        label_en="Tier 5 — VIP",
        required_points=10**9,  # Tier 5 cannot be purchased with points; VIP/admin only
        max_files_normal=0,  # Not available to non-VIP/admin
        max_files_vip=20,
        description_ar="حصرية لـ VIP والأدمن. حتى 20 ملف لكل مستخدم VIP.",
        description_en="VIP/admin only. Up to 20 files per VIP user.",
    ),
)


def by_level(level: int) -> Tier:
    for t in TIERS:
        if t.level == level:
            return t
    return TIERS[0]


def unlocked_tiers(points: int, *, is_vip: bool, is_admin: bool) -> list[Tier]:
    """Return list of tier levels the user can currently use."""
    out: list[Tier] = []
    for t in TIERS:
        if t.level == 5:
            if is_vip or is_admin:
                out.append(t)
            continue
        if points >= t.required_points:
            out.append(t)
    return out


def max_files_for(tier: Tier, *, is_vip: bool, is_admin: bool) -> int:
    """How many files this user may host on this tier."""
    if is_admin:
        # Admin: unlimited per tier
        return 10**9
    if is_vip:
        return tier.max_files_vip
    return tier.max_files_normal


def can_use_tier(tier: Tier, points: int, *, is_vip: bool, is_admin: bool) -> bool:
    if is_admin:
        return True
    if tier.level == 5:
        return is_vip
    return points >= tier.required_points
