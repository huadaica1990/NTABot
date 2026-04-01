"""
TroopBrain - Rule engine for army health analysis and decisions.
"""
import time
from typing import Callable

from config import BotConfig


class TroopBrain:
    """Rule engine phan tich mau dao quan va de xuat hanh dong."""
    def __init__(self, cfg: BotConfig, log: Callable):
        self.cfg = cfg
        self.log = log
        self.history = {}

    def analyze(self, troop: dict, next_label: str = "") -> dict:
        name = troop.get('name', '?')
        avg_hp = float(troop.get('avg_hp', 1.0) or 1.0)
        min_hp = float(troop.get('min_hp', avg_hp) or avg_hp)
        critical_units = int(troop.get('critical_units', 0) or 0)
        unit_count = max(1, int(troop.get('unit_count', 1) or 1))
        hist = self.history.setdefault(name, [])
        prev_avg = hist[-1]['avg_hp'] if hist else avg_hp
        hp_trend = round(avg_hp - prev_avg, 3)
        hist.append({'ts': time.time(), 'avg_hp': avg_hp, 'min_hp': min_hp})
        if len(hist) > 8:
            del hist[:-8]

        lv_penalty = 0
        ll = (next_label or '').lower()
        if 'lv4' in ll or 'lv.4' in ll:
            lv_penalty = 18
        elif 'lv3' in ll or 'lv.3' in ll:
            lv_penalty = 10
        elif 'lv2' in ll or 'lv.2' in ll:
            lv_penalty = 4

        risk = int((1.0 - avg_hp) * 45 + (critical_units / unit_count) * 30 + max(0.0, -hp_trend) * 35 + lv_penalty)

        if avg_hp <= self.cfg.health_force_heal_avg or critical_units >= self.cfg.health_force_heal_critical_units:
            action = 'heal_now'
        elif min_hp <= self.cfg.health_critical_ratio and lv_penalty >= 10:
            action = 'heal_now'
        elif avg_hp <= self.cfg.health_warn_avg:
            action = 'low_risk_only'
        else:
            action = 'continue'

        return {
            'name': name,
            'avg_hp': avg_hp,
            'min_hp': min_hp,
            'critical_units': critical_units,
            'unit_count': unit_count,
            'hp_trend': hp_trend,
            'risk_score': risk,
            'action': action,
            'status': troop.get('status', 'Unknown'),
        }

    def summarize(self, troops: list, next_label: str = '') -> dict:
        analyses = [self.analyze(t, next_label=next_label) for t in troops]
        force_heal = any(a['action'] == 'heal_now' for a in analyses)
        low_risk_only = (not force_heal) and any(a['action'] == 'low_risk_only' for a in analyses)
        return {
            'troops': analyses,
            'force_heal': force_heal,
            'low_risk_only': low_risk_only,
        }

