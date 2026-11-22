"""Owner-only interface to a separately installed privileged recovery component.

There is deliberately no disk deletion fallback in the till application.
"""
import json
from pathlib import Path
import subprocess

HELPER = Path('/usr/local/lib/salonmax-groundhog/control.py')


def owner_allowed(repo, staff):
    if staff is None:
        return False
    row = repo.fetch_one('select role,is_active from staff_users where id=?', (int(staff['id']),))
    return bool(row and row['is_active'] and str(row['role']).lower() == 'owner')


def hardware_idle(window):
    state = window.runtime_state
    return (not any(state.get('bed_queues', {}).values())
            and not any(state.get('sale_baskets', {}).values())
            and not state.get('pending_sale')
            and all(bed.get('current_phase', 'ready') == 'ready'
                    for bed in state.get('bed_states', {}).values()))


def helper_request(body):
    if not HELPER.is_file():
        return {'ok': False, 'message': 'Full SD-card recovery erasure is not installed. Nothing has been erased.'}
    try:
        result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/bin/python3', str(HELPER)],
                                input=json.dumps(body), text=True, capture_output=True, timeout=20)
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError('Invalid helper response')
        return payload
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'ok': False, 'message': 'The protected recovery component is unavailable. Nothing has been erased.'}


def show(window):
    # Import Qt lazily so authorization and safety checks can be tested headlessly.
    try:
        from PySide6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QMessageBox
    except ImportError:
        from PyQt6.QtWidgets import QDialog, QFormLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QMessageBox
    if not owner_allowed(window.repo, window.session.staff_row):
        QMessageBox.warning(window, 'Access restricted', 'Ground Hog requires an owner sign-in.')
        return
    if not hardware_idle(window):
        QMessageBox.warning(window, 'Till in use', 'All beds, queues and sale baskets must be idle first.')
        return
    status = helper_request({'action': 'check'})
    if not status.get('ok'):
        QMessageBox.warning(window, 'Ground Hog unavailable', status.get('message', 'Recovery is unavailable.'))
        return
    dialog = QDialog(window)
    dialog.setWindowTitle('Ground Hog — erase entire SD card')
    layout = QVBoxLayout(dialog)
    warning = QLabel('This will restart into a memory-only eraser and overwrite the ENTIRE Pi SD card, '
                     'including the operating system, local accounts, minutes, passwords and local backups. '
                     'The Pi will need a full reinstall. The separate backup on your PC is kept. '
                     'Keep power connected until erasure finishes; a large card may take hours. '
                     'This is not a certified forensic erase of flash memory.')
    warning.setWordWrap(True)
    layout.addWidget(warning)
    layout.addWidget(QLabel('Verified PC backup: ' + status.get('backup_at', 'unknown')))
    form = QFormLayout()
    owner_pin, manager_pin, phrase = QLineEdit(), QLineEdit(), QLineEdit()
    owner_pin.setEchoMode(QLineEdit.EchoMode.Password)
    manager_pin.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow('Confirm owner PIN', owner_pin)
    form.addRow('Manager or owner PIN', manager_pin)
    form.addRow('Type ERASE SD CARD', phrase)
    layout.addLayout(form)
    cancel = QPushButton('Cancel')
    cancel.clicked.connect(dialog.reject)
    layout.addWidget(cancel)
    erase = QPushButton('Erase entire SD card permanently')
    layout.addWidget(erase)

    def confirm():
        if not owner_allowed(window.repo, window.session.staff_row) or not hardware_idle(window):
            QMessageBox.warning(dialog, 'Blocked', 'Owner access and idle beds are required.')
            return
        if phrase.text() != 'ERASE SD CARD':
            QMessageBox.warning(dialog, 'Confirmation required', 'Type ERASE SD CARD exactly.')
            return
        answer = QMessageBox.warning(dialog, 'Final irreversible confirmation',
            'Permanently overwrite this Pi SD card and make this till unbootable?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = helper_request({'action': 'arm', 'owner_id': int(window.session.staff_row['id']),
                                 'owner_pin': owner_pin.text(), 'manager_pin': manager_pin.text(),
                                 'confirmation': phrase.text()})
        owner_pin.clear()
        manager_pin.clear()
        if not result.get('ok'):
            QMessageBox.warning(dialog, 'Erasure blocked', result.get('message', 'Nothing has been erased.'))
            return
        erase.setEnabled(False)
        dialog.accept()
        QMessageBox.information(window, 'Recovery restart scheduled',
            'The protected recovery process has accepted the request. Keep the Pi powered on.')

    erase.clicked.connect(confirm)
    dialog.exec()
