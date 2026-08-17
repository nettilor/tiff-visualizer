"""Black theme suited to microscopy: near-black chrome, pure black image
backdrops, black dividers."""

from __future__ import annotations

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

_CHECK_SVG = (Path(__file__).parent / "assets" / "check.svg").as_posix()


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")

    window = QColor("#0a0a0a")
    base = QColor("#050505")
    button = QColor("#181818")
    text = QColor("#d0d0d0")
    dim = QColor("#6a6a6a")
    highlight = QColor("#2f6fd0")

    p = QPalette()
    p.setColor(QPalette.Window, window)
    p.setColor(QPalette.WindowText, text)
    p.setColor(QPalette.Base, base)
    p.setColor(QPalette.AlternateBase, QColor("#101010"))
    p.setColor(QPalette.Text, text)
    p.setColor(QPalette.Button, button)
    p.setColor(QPalette.ButtonText, text)
    p.setColor(QPalette.BrightText, QColor("#ffffff"))
    p.setColor(QPalette.Highlight, highlight)
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.ToolTipBase, QColor("#181818"))
    p.setColor(QPalette.ToolTipText, text)
    p.setColor(QPalette.PlaceholderText, dim)
    p.setColor(QPalette.Link, highlight)
    p.setColor(QPalette.Mid, QColor("#2a2a2a"))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        p.setColor(QPalette.Disabled, role, dim)
    app.setPalette(p)

    app.setStyleSheet(
        f"""
        QSplitter::handle {{ background: #000000; }}
        QSplitter::handle:hover {{ background: #2a2a2a; }}
        QToolTip {{ background: #181818; color: #d0d0d0; border: 1px solid #333333; }}
        QCheckBox::indicator, QRadioButton::indicator {{
            width: 14px; height: 14px;
            border: 1px solid #6a6a6a;
            background: #3a3a3a;
        }}
        QCheckBox::indicator {{ border-radius: 3px; }}
        QRadioButton::indicator {{ border-radius: 7px; }}
        QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
            border-color: #9a9a9a;
        }}
        QCheckBox::indicator:checked {{
            background: #2f6fd0;
            border-color: #2f6fd0;
            image: url({_CHECK_SVG});
        }}
        QRadioButton::indicator:checked {{
            background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                stop:0.55 #ffffff, stop:0.65 #2f6fd0, stop:1 #2f6fd0);
            border-color: #2f6fd0;
        }}
        QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
            background: #222222;
            border-color: #3a3a3a;
        }}
        """
    )

    # Pure black behind the images themselves.
    pg.setConfigOptions(background="#000000")
