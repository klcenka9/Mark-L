"""
HUD review dialogs for pending self-improvement changes.

PyQt6 counterpart of scripts/review_changes.py — both drive the same
protected backend (core/review_gate.py). Two surfaces:

  PendingChangesDialog  — list of waiting diffs; ordinary DANGEROUS changes
                          are approved/rejected here with a plain yes/no.
  CoreSafetyDialog      — SEPARATE red screen for core_safety_change:
                          * approve button stays locked until the reviewer
                            scrolls through the ENTIRE diff/rationale/rollback
                            AND a time delay has elapsed,
                          * first click reveals a second confirmation inside
                            the same window; only the second click approves,
                          * every approval is audit-logged with approved_by,
                            timestamp and diff hash (done in review_gate).

This file is in PROTECTED_PATHS — the agent's pipeline cannot weaken
these mechanics.
"""

import getpass
import json
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core import review_gate, self_mod

_MONO   = "Courier New"
_RED    = "#ff3355"
_RED_BG = "#1a0008"
_DIM    = "#5ab8cc"
_BG     = "#010d14"
_BORDER = "#0d3347"


def _reviewer_name() -> str:
    """Human identity for the audit log: configured user name, else OS user."""
    try:
        cfg = json.loads(
            (self_mod.BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        if (cfg.get("user_name") or "").strip():
            return cfg["user_name"].strip()
    except Exception:
        pass
    return getpass.getuser()


def _mono_text(read_only_text: str) -> QPlainTextEdit:
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setPlainText(read_only_text)
    box.setFont(QFont(_MONO, 9))
    box.setStyleSheet(f"background: {_BG}; color: #d8f8ff; border: 1px solid {_BORDER};")
    return box


class CoreSafetyDialog(QDialog):
    """Separate red screen for a single core_safety_change record."""

    def __init__(self, record: dict, parent=None):
        super().__init__(parent)
        self.record    = record
        self._scrolled = False
        self._armed    = False   # True after first click — second click approves
        self._shown_at = time.monotonic()

        self.setWindowTitle("CORE SAFETY CHANGE — approval required")
        self.setModal(True)
        self.resize(860, 640)
        self.setStyleSheet(f"background: {_RED_BG}; border: 2px solid {_RED};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        title = QLabel("⚠  CORE SAFETY CHANGE")
        title.setFont(QFont(_MONO, 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {_RED}; background: transparent;")
        lay.addWidget(title)

        sub = QLabel("This diff modifies the safety mechanism of the agent itself. "
                     "Read the ENTIRE diff, rationale and rollback plan below — "
                     "the approve button unlocks only after you scroll to the end.")
        sub.setWordWrap(True)
        sub.setFont(QFont(_MONO, 9))
        sub.setStyleSheet("color: #ffaabb; background: transparent;")
        lay.addWidget(sub)

        self._text = _mono_text(review_gate.review_text(record))
        self._text.setStyleSheet(
            f"background: #0d0004; color: #ffd8de; border: 1px solid {_RED};")
        self._text.verticalScrollBar().valueChanged.connect(self._check_scrolled)
        lay.addWidget(self._text, stretch=1)

        self._confirm_lbl = QLabel(review_gate.CORE_SAFETY_WARNING)
        self._confirm_lbl.setWordWrap(True)
        self._confirm_lbl.setFont(QFont(_MONO, 11, QFont.Weight.Bold))
        self._confirm_lbl.setStyleSheet(f"color: {_RED}; background: transparent;")
        self._confirm_lbl.hide()
        lay.addWidget(self._confirm_lbl)

        row = QHBoxLayout()
        self._status_lbl = QLabel()
        self._status_lbl.setFont(QFont(_MONO, 9))
        self._status_lbl.setStyleSheet("color: #ffaabb; background: transparent;")
        row.addWidget(self._status_lbl, stretch=1)

        cancel = QPushButton("CANCEL")
        cancel.setFont(QFont(_MONO, 9, QFont.Weight.Bold))
        cancel.setFixedHeight(34)
        cancel.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_DIM}; "
            f"border: 1px solid {_BORDER}; border-radius: 3px; padding: 0 14px; }}")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)

        self._approve_btn = QPushButton()
        self._approve_btn.setFont(QFont(_MONO, 9, QFont.Weight.Bold))
        self._approve_btn.setFixedHeight(34)
        self._approve_btn.clicked.connect(self._on_approve_clicked)
        row.addWidget(self._approve_btn)
        lay.addLayout(row)

        # Unlock ticker: re-evaluates scroll + delay once a second.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_lock)
        self._timer.start(500)
        self._check_scrolled()
        self._update_lock()

    # ── locking mechanics ────────────────────────────────────────────────────

    def _check_scrolled(self, *_):
        bar = self._text.verticalScrollBar()
        if bar.maximum() == 0 or bar.value() >= bar.maximum():
            self._scrolled = True

    def _remaining_delay(self) -> float:
        return max(0.0, review_gate.CORE_SAFETY_DELAY_S
                   - (time.monotonic() - self._shown_at))

    def _update_lock(self):
        if self._armed:
            return
        wait    = self._remaining_delay()
        unlocked = self._scrolled and wait <= 0
        self._approve_btn.setEnabled(unlocked)
        if unlocked:
            self._approve_btn.setText("APPROVE CORE SAFETY CHANGE")
            self._approve_btn.setStyleSheet(
                f"QPushButton {{ background: #2a000f; color: {_RED}; "
                f"border: 1px solid {_RED}; border-radius: 3px; padding: 0 14px; }}"
                f"QPushButton:hover {{ background: #40001a; }}")
            self._status_lbl.setText("")
        else:
            reasons = []
            if not self._scrolled:
                reasons.append("scroll to the end of the diff")
            if wait > 0:
                reasons.append(f"wait {wait:.0f}s")
            self._approve_btn.setText("APPROVE  (locked)")
            self._approve_btn.setStyleSheet(
                "QPushButton { background: #14090c; color: #663344; "
                "border: 1px solid #663344; border-radius: 3px; padding: 0 14px; }")
            self._status_lbl.setText("Locked: " + " and ".join(reasons))

    # ── two-step confirmation ────────────────────────────────────────────────

    def _on_approve_clicked(self):
        if not self._armed:
            self._armed = True
            self._confirm_lbl.show()
            self._approve_btn.setText("YES — APPLY CORE SAFETY CHANGE")
            self._status_lbl.setText("Second confirmation required.")
            return

        try:
            result = review_gate.approve_and_apply_core_safety(
                self.record["id"], _reviewer_name())
        except Exception as e:
            QMessageBox.critical(self, "Core safety approval failed", str(e))
            return
        QMessageBox.information(self, "Core safety change", result)
        self.accept()


class PendingChangesDialog(QDialog):
    """List + detail of all pending changes awaiting human review."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Self-improvement — pending changes")
        self.setModal(True)
        self.resize(900, 620)
        self.setStyleSheet(f"background: {_BG};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(7)

        hdr = QLabel("▸ PENDING SELF-IMPROVEMENT CHANGES")
        hdr.setFont(QFont(_MONO, 10, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {_DIM}; background: transparent;")
        lay.addWidget(hdr)

        self._list = QListWidget()
        self._list.setFont(QFont(_MONO, 9))
        self._list.setFixedHeight(150)
        self._list.setStyleSheet(
            f"background: #000d14; color: #d8f8ff; border: 1px solid {_BORDER};")
        self._list.currentItemChanged.connect(self._show_detail)
        lay.addWidget(self._list)

        self._detail = _mono_text("")
        lay.addWidget(self._detail, stretch=1)

        row = QHBoxLayout()
        row.addStretch(1)

        self._reject_btn = QPushButton("REJECT…")
        self._reject_btn.setFont(QFont(_MONO, 9, QFont.Weight.Bold))
        self._reject_btn.setFixedHeight(32)
        self._reject_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {_DIM}; "
            f"border: 1px solid {_BORDER}; border-radius: 3px; padding: 0 14px; }}")
        self._reject_btn.clicked.connect(self._reject_selected)
        row.addWidget(self._reject_btn)

        self._approve_btn = QPushButton("APPROVE")
        self._approve_btn.setFont(QFont(_MONO, 9, QFont.Weight.Bold))
        self._approve_btn.setFixedHeight(32)
        self._approve_btn.clicked.connect(self._approve_selected)
        row.addWidget(self._approve_btn)
        lay.addLayout(row)

        self._reload()

    # ── data ─────────────────────────────────────────────────────────────────

    def _reload(self):
        self._list.clear()
        for rec in self_mod.list_pending():
            if rec["status"] != "pending":
                continue
            risk = review_gate.actual_risk(rec)   # content-derived, not stored
            tag  = "⚠ CORE SAFETY" if risk == "core_safety_change" else risk.upper()
            item = QListWidgetItem(f"[{tag}]  {rec['id']}  —  {rec['rationale'][:60]}")
            item.setData(Qt.ItemDataRole.UserRole, (rec, risk))
            if risk == "core_safety_change":
                item.setForeground(Qt.GlobalColor.red)
            self._list.addItem(item)
        if self._list.count() == 0:
            self._detail.setPlainText("No changes are waiting for approval.")
            self._approve_btn.setEnabled(False)
            self._reject_btn.setEnabled(False)
        else:
            self._list.setCurrentRow(0)
            self._approve_btn.setEnabled(True)
            self._reject_btn.setEnabled(True)

    def _current(self):
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else (None, None)

    def _show_detail(self, *_):
        rec, risk = self._current()
        if not rec:
            return
        self._detail.setPlainText(review_gate.review_text(rec))
        if risk == "core_safety_change":
            self._approve_btn.setText("OPEN CORE SAFETY REVIEW…")
            self._approve_btn.setStyleSheet(
                f"QPushButton {{ background: #2a000f; color: {_RED}; "
                f"border: 1px solid {_RED}; border-radius: 3px; padding: 0 14px; }}")
        else:
            self._approve_btn.setText("APPROVE")
            self._approve_btn.setStyleSheet(
                "QPushButton { background: #001a10; color: #00ff88; "
                "border: 1px solid #00aa55; border-radius: 3px; padding: 0 14px; }")

    # ── actions ──────────────────────────────────────────────────────────────

    def _approve_selected(self):
        rec, risk = self._current()
        if not rec:
            return
        if risk == "core_safety_change":
            # Core safety NEVER goes through the ordinary yes/no below —
            # it gets its own screen with heavier mechanics.
            dlg = CoreSafetyDialog(rec, parent=self)
            dlg.exec()
            self._reload()
            return

        answer = QMessageBox.question(
            self, "Approve dangerous change",
            f"Approve and apply this DANGEROUS change?\n\n"
            f"{rec['rationale'][:200]}\n\nRollback: {rec.get('rollback_plan', 'git revert')}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = review_gate.approve_dangerous(rec["id"], _reviewer_name())
        except Exception as e:
            QMessageBox.critical(self, "Approval failed", str(e))
            return
        QMessageBox.information(self, "Change approved", result)
        self._reload()

    def _reject_selected(self):
        rec, _risk = self._current()
        if not rec:
            return
        reason, ok = QInputDialog.getText(
            self, "Reject change", "Reason for rejection (logged for the agent):")
        if not ok or not reason.strip():
            return
        review_gate.reject_change(rec["id"], reason.strip(), _reviewer_name())
        self._reload()
