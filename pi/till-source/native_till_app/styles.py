APP_STYLESHEET = """
QWidget {
    background: #f4f7fb;
    color: #183247;
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 17px;
}
QMainWindow, QDialog {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #f7fbff,
        stop:0.45 #eef5fb,
        stop:1 #e8f0f8
    );
}
QFrame#Card {
    background: rgba(255, 255, 255, 0.97);
    border: 1px solid #d6e2ee;
    border-radius: 24px;
}
QFrame#HeroCard {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(255,255,255,0.99),
        stop:1 rgba(247,250,253,0.99)
    );
    border: 1px solid #c7d9e9;
    border-radius: 30px;
}
QLabel[role="title"] {
    font-size: 38px;
    font-weight: 700;
    color: #102f45;
}
QLabel[role="section_title"] {
    font-size: 25px;
    font-weight: 700;
    color: #163d59;
}
QLabel[role="muted"] {
    color: #667887;
    font-size: 16px;
    font-weight: 500;
}
QLabel[role="eyebrow"] {
    color: #6d7f8d;
    font-size: 13px;
    font-weight: 600;
}
QLabel[role="metric"] {
    font-size: 32px;
    font-weight: 700;
    color: #0f4c6e;
}
QPushButton {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #6d7f8d,
        stop:1 #51616d
    );
    color: white;
    border: 1px solid #42505a;
    border-radius: 16px;
    padding: 14px 22px;
    font-size: 16px;
    font-weight: 600;
    min-height: 22px;
}
QPushButton:hover {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #7b8d9b,
        stop:1 #5c6d79
    );
}
QPushButton:checked {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #0f6f73,
        stop:1 #0b5960
    );
    color: white;
    border: 3px solid #c62828;
}
QPushButton[variant="secondary"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #6d7f8d,
        stop:1 #51616d
    );
    border: 1px solid #42505a;
}
QPushButton[variant="accent"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #6d7f8d,
        stop:1 #51616d
    );
    border: 1px solid #42505a;
}
QPushButton[variant="success"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #3d9d58,
        stop:1 #28733d
    );
    border: 1px solid #215f33;
}
QPushButton[variant="danger"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #df5d55,
        stop:1 #b93028
    );
    border: 1px solid #8f231d;
}
QPushButton[variant="warning"] {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #f1ba48,
        stop:1 #d58b09
    );
    border: 1px solid #a86d07;
}
QPushButton:disabled {
    background: #c8d5e1;
    color: #6f8190;
    border: 1px solid #b8c6d2;
}
QPushButton[navtab="true"] {
    background: #eff4f9;
    color: #24455d;
    border: 1px solid #d5e0ea;
    border-radius: 18px;
    padding: 8px 13px;
    font-size: 13px;
    font-weight: 600;
    min-height: 14px;
}
QPushButton[navtab="true"]:hover {
    background: #e5eef7;
    color: #16384d;
}
QPushButton[navtab="true"]:checked {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 #0f4c6e,
        stop:1 #0a6588
    );
    color: white;
    border: 1px solid #0b435f;
}
QPushButton[bedcard="true"] {
    text-align: center;
    padding: 14px 12px;
    min-height: 68px;
    font-size: 19px;
    line-height: 1.25;
    font-weight: 500;
    letter-spacing: 0.3px;
}
QPushButton[compact="true"] {
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 600;
    min-height: 16px;
}
QFrame[panel="muted"] {
    background: #f3f7fb;
    border: 1px solid #d9e4ee;
    border-radius: 20px;
}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {
    background: white;
    border: 1px solid #c6d5e3;
    border-radius: 16px;
    padding: 12px 14px;
    font-size: 17px;
    font-weight: 500;
    min-height: 24px;
    selection-background-color: #d9ebff;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus {
    border: 2px solid #1a73d1;
    padding: 11px 13px;
}
QProgressBar {
    border: 1px solid #d3dee9;
    border-radius: 12px;
    background: #eef3f7;
    text-align: center;
    color: #163247;
    font-weight: 600;
    min-height: 20px;
}
QProgressBar::chunk {
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 #3d9d58,
        stop:1 #1a73d1
    );
    border-radius: 10px;
}
QTableWidget {
    background: white;
    border: 1px solid #d5e0ea;
    border-radius: 18px;
    gridline-color: #edf2f7;
    alternate-background-color: #f8fbfd;
}
QHeaderView::section {
    background: #eff5fa;
    color: #17384d;
    padding: 12px;
    border: none;
    border-bottom: 1px solid #d5e0ea;
    font-size: 15px;
    font-weight: 600;
}
QListWidget {
    background: white;
    border: 1px solid #d5e0ea;
    border-radius: 18px;
    padding: 10px;
}
QListWidget::item {
    padding: 10px 12px;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 500;
}
QListWidget::item:selected {
    background: #d9ebff;
    color: #102f45;
}
"""
