import sys
import os
import re
import difflib
import threading


def _is_libpng_iccp_warning(text: str) -> bool:
    return 'libpng warning' in text or 'iCCP' in text and 'sRGB profile' in text


def _suppress_libpng_iccp_warnings():
    """过滤 Qt 内置 PNG 触发的 libpng iCCP 警告（C 层与 Python 层 stderr）。"""
    if getattr(_suppress_libpng_iccp_warnings, '_installed', False):
        return
    _suppress_libpng_iccp_warnings._installed = True

    _stderr = sys.stderr

    class _PyFilter:
        def write(self, text):
            if _is_libpng_iccp_warning(text):
                return
            _stderr.write(text)

        def flush(self):
            _stderr.flush()

        def fileno(self):
            return _stderr.fileno()

    sys.stderr = _PyFilter()

    try:
        read_fd, write_fd = os.pipe()
        saved_fd = os.dup(2)
    except OSError:
        return

    os.dup2(write_fd, 2)
    os.close(write_fd)

    def _forward_stderr():
        try:
            with os.fdopen(read_fd, 'r', encoding='utf-8', errors='replace') as src:
                with os.fdopen(saved_fd, 'w', encoding='utf-8', errors='replace') as dst:
                    while True:
                        line = src.readline()
                        if not line:
                            break
                        if _is_libpng_iccp_warning(line):
                            continue
                        dst.write(line)
                        dst.flush()
        except OSError:
            pass

    threading.Thread(target=_forward_stderr, daemon=True).start()


_suppress_libpng_iccp_warnings()

from pathlib import Path


def _norm_path(path: str) -> str:
    return os.path.normpath(os.path.abspath(path))
from typing import List, Dict
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QListWidget, QAbstractItemView,
    QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit, QDialog, QFormLayout,
    QDialogButtonBox, QProgressBar, QMessageBox, QPlainTextEdit, QSplitter, QTextEdit,
    QCheckBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRect
from PyQt6.QtGui import (
    QFont, QKeySequence, QShortcut, QTextCursor, QColor, QPainter, QTextFormat,
    QTextCharFormat
)


# ====================== 数据结构 ======================
class Operation:
    def __init__(self, op_type, params, description):
        self.type = op_type
        self.params = params
        self.description = description


# ====================== NC 处理核心（包含所有操作类型） ======================
class NCProcessor:
    @staticmethod
    def edit_line_text(line: str, position: str, custom_pos: int, text: str, mode: str) -> str:
        if mode == 'delete':
            return line.replace(text, '', 1)
        else:
            if position == 'start':
                return text + line
            elif position == 'end':
                return line.rstrip('\n') + text + ('\n' if line.endswith('\n') else '')
            else:
                idx = min(custom_pos, len(line))
                return line[:idx] + text + line[idx:]

    @staticmethod
    def swap_xy(line: str) -> str:
        ending = _line_ending(line)
        content = line[:-len(ending)] if ending else line
        coords = re.findall(r'([XYZ])([+-]?\d*\.?\d+)', content)
        coord_dict = {axis: val for axis, val in coords}
        if 'X' in coord_dict and 'Y' in coord_dict:
            new_x = coord_dict['Y']
            new_y = coord_dict['X']
            content = re.sub(r'X[+-]?\d*\.?\d+', f'X{new_x}', content)
            content = re.sub(r'Y[+-]?\d*\.?\d+', f'Y{new_y}', content)
        elif 'X' in coord_dict:
            x_val = coord_dict['X']
            content = re.sub(r'X[+-]?\d*\.?\d+', '', content)
            content = content.rstrip(' \t') + f' Y{x_val}'
        elif 'Y' in coord_dict:
            y_val = coord_dict['Y']
            content = re.sub(r'Y[+-]?\d*\.?\d+', '', content)
            content = content.rstrip(' \t') + f' X{y_val}'
        content = re.sub(r'[ \t]+', ' ', content).strip(' \t')
        return content + ending

    @staticmethod
    def transform_axes(line: str, dx: float, dy: float, dz: float,
                       invert_x: bool, invert_y: bool, invert_z: bool) -> str:
        axes = {'X': (dx, invert_x), 'Y': (dy, invert_y), 'Z': (dz, invert_z)}
        for axis, (delta, invert) in axes.items():
            pattern = rf'({axis})([+-]?\d*\.?\d+)'
            def repl(match, ax=axis, d=delta, inv=invert):
                val = float(match.group(2))
                if inv:
                    val = -val
                val += d
                formatted = f"{val:.6f}".rstrip('0').rstrip('.')
                return f"{ax}{formatted}"
            line = re.sub(pattern, repl, line)
        return line

    @staticmethod
    def apply_tool_offset(file_lines: List[str], tool_number: int,
                          offset_x: float, offset_y: float, offset_z: float) -> List[str]:
        blocks, current_tool, block_start = [], None, 0
        for i, line in enumerate(file_lines):
            match = re.search(r'T(\d+)', line)
            if match:
                if current_tool is not None:
                    blocks.append((block_start, i, current_tool))
                current_tool = int(match.group(1))
                block_start = i
        if current_tool is not None:
            blocks.append((block_start, len(file_lines), current_tool))
        if not blocks:
            return file_lines
        target_blocks = [(s, e) for s, e, t in blocks if t == tool_number]
        if not target_blocks:
            return file_lines
        modified = file_lines.copy()
        for s, e in target_blocks:
            for idx in range(s, e):
                modified[idx] = NCProcessor.transform_axes(modified[idx], offset_x, offset_y, offset_z, False, False, False)
        return modified

    @staticmethod
    def text_replace(line: str, find: str, replace: str) -> str:
        return line.replace(find, replace)

    @staticmethod
    def apply_operations(lines: List[str], ops: List[Operation]) -> List[str]:
        modified = lines[:]
        for op in ops:
            if op.type == 'reset':
                modified = lines[:]
            elif op.type == 'edit_line_text':
                pos = op.params['position']
                custom = op.params.get('custom_pos', 0)
                text = op.params['text']
                mode = op.params['mode']
                modified = [NCProcessor.edit_line_text(line, pos, custom, text, mode) for line in modified]
            elif op.type == 'swap_xy':
                modified = [NCProcessor.swap_xy(line) for line in modified]
            elif op.type == 'transform':
                dx = op.params.get('dx',0); dy = op.params.get('dy',0); dz = op.params.get('dz',0)
                ix = op.params.get('invert_x',False); iy = op.params.get('invert_y',False); iz = op.params.get('invert_z',False)
                modified = [NCProcessor.transform_axes(line, dx, dy, dz, ix, iy, iz) for line in modified]
            elif op.type == 'tool_offset':
                tool = op.params['tool']
                ox = op.params.get('offset_x',0); oy = op.params.get('offset_y',0); oz = op.params.get('offset_z',0)
                modified = NCProcessor.apply_tool_offset(modified, tool, ox, oy, oz)
            elif op.type == 'replace':
                find, repl = op.params['find'], op.params['replace']
                modified = [NCProcessor.text_replace(line, find, repl) for line in modified]
        return modified

    @staticmethod
    def load_file(path):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return f.readlines(), True, ""
        except Exception as e:
            return [], False, str(e)

    @staticmethod
    def save_file(path, lines):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            return True, ""
        except Exception as e:
            return False, str(e)


# ====================== 精确高亮（仅标红变化部分） ======================
def _line_ending(line: str) -> str:
    if line.endswith('\r\n'):
        return '\r\n'
    if line.endswith('\n'):
        return '\n'
    if line.endswith('\r'):
        return '\r'
    return ''


def _insert_char_diff(cursor: QTextCursor, original: str, modified: str,
                      fmt_normal: QTextCharFormat, fmt_highlight: QTextCharFormat):
    if original == modified:
        cursor.insertText(modified, fmt_normal)
        return
    matcher = difflib.SequenceMatcher(None, original, modified)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        fmt = fmt_highlight if tag != 'equal' else fmt_normal
        cursor.insertText(modified[j1:j2], fmt)


# ====================== 带行号的文本编辑区（官方示例，稳定） ======================
class LineNumberTextEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)
        self.update_line_number_area_width()

    def update_line_number_area_width(self):
        digits = len(str(self.blockCount()))
        space = self.fontMetrics().horizontalAdvance('9') * (digits + 2)
        self.setViewportMargins(space, 0, 0, 0)
        self.line_number_area.setFixedWidth(space)

    def update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(QRect(cr.left(), cr.top(), self.line_number_area.width(), cr.height()))

    def line_number_area_paint_event(self, event):
        with QPainter(self.line_number_area) as painter:
            painter.fillRect(event.rect(), QColor(240, 240, 240))
            block = self.firstVisibleBlock()
            block_number = block.blockNumber() + 1
            top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            bottom = top + self.blockBoundingRect(block).height()
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible() and bottom >= event.rect().top():
                    painter.setPen(Qt.GlobalColor.gray)
                    painter.drawText(
                        0, int(top), self.line_number_area.width(), self.fontMetrics().height(),
                        Qt.AlignmentFlag.AlignRight, str(block_number)
                    )
                block = block.next()
                if not block.isValid():
                    break
                top = bottom
                bottom = top + self.blockBoundingRect(block).height()
                block_number += 1

    def highlight_current_line(self):
        extra_selections = []
        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(230, 240, 255))
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)
        self.setExtraSelections(extra_selections)

    def set_plain_text_with_diff(self, lines: List[str], orig_lines: List[str]):
        """QPlainTextEdit 不支持 HTML，用字符格式实现差异高亮。"""
        self.blockSignals(True)
        self.clear()
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        fmt_normal = QTextCharFormat()
        fmt_highlight = QTextCharFormat()
        fmt_highlight.setBackground(QColor(255, 204, 204))
        max_len = max(len(orig_lines), len(lines))
        for i in range(max_len):
            line = lines[i] if i < len(lines) else ''
            orig = orig_lines[i].rstrip('\n\r') if i < len(orig_lines) else ''
            curr = line.rstrip('\n\r')
            suffix = _line_ending(line)
            if orig != curr:
                _insert_char_diff(cursor, orig, curr, fmt_normal, fmt_highlight)
                cursor.insertText(suffix, fmt_normal)
            else:
                cursor.insertText(line, fmt_normal)
        self.blockSignals(False)


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


# ====================== 同步滚动（安全实现） ======================
class SyncTextEdit(LineNumberTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sync_partner = None
        self._scrolling = False

    def set_sync_partner(self, partner):
        if self._sync_partner == partner:
            return
        self._sync_partner = partner
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _on_scroll(self, value):
        if self._sync_partner and not self._sync_partner._scrolling:
            self._sync_partner._scrolling = True
            self._sync_partner.verticalScrollBar().setValue(value)
            self._sync_partner._scrolling = False


# ====================== 操作弹窗（预创建所有控件，稳定） ======================
class OperationDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加操作")
        self.setMinimumWidth(500)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("操作类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems([
            "行文本编辑（插入/删除）",
            "坐标轴交换 (X↔Y)",
            "坐标轴变换 (平移/反向)",
            "刀具偏移 (按刀号)",
            "文本替换"
        ])
        layout.addWidget(self.type_combo)

        self.param_container = QWidget()
        self.param_layout = QFormLayout(self.param_container)

        # 行文本编辑
        self.pos_combo = QComboBox()
        self.pos_combo.addItems(['行首', '行尾', '指定列'])
        self.custom_spin = QSpinBox()
        self.custom_spin.setRange(0, 9999)
        self.custom_spin.setVisible(False)
        self.text_edit = QLineEdit()
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(['插入', '删除'])

        # 坐标轴变换
        self.dx_spin = QDoubleSpinBox(); self.dx_spin.setRange(-1e4,1e4); self.dx_spin.setDecimals(3)
        self.dy_spin = QDoubleSpinBox(); self.dy_spin.setRange(-1e4,1e4); self.dy_spin.setDecimals(3)
        self.dz_spin = QDoubleSpinBox(); self.dz_spin.setRange(-1e4,1e4); self.dz_spin.setDecimals(3)
        self.inv_x = QCheckBox("X反向")
        self.inv_y = QCheckBox("Y反向")
        self.inv_z = QCheckBox("Z反向")

        # 刀具偏移
        self.tool_spin = QSpinBox(); self.tool_spin.setRange(1,999)
        self.ox_spin = QDoubleSpinBox(); self.ox_spin.setRange(-1e4,1e4); self.ox_spin.setDecimals(3)
        self.oy_spin = QDoubleSpinBox(); self.oy_spin.setRange(-1e4,1e4); self.oy_spin.setDecimals(3)
        self.oz_spin = QDoubleSpinBox(); self.oz_spin.setRange(-1e4,1e4); self.oz_spin.setDecimals(3)

        # 文本替换
        self.find_edit = QLineEdit()
        self.replace_edit = QLineEdit()

        # 全部加入布局，初始隐藏
        self.param_layout.addRow("位置:", self.pos_combo)
        self.param_layout.addRow("索引(0起):", self.custom_spin)
        self.param_layout.addRow("文本:", self.text_edit)
        self.param_layout.addRow("模式:", self.mode_combo)
        self.param_layout.addRow("X平移:", self.dx_spin)
        self.param_layout.addRow("Y平移:", self.dy_spin)
        self.param_layout.addRow("Z平移:", self.dz_spin)
        self.param_layout.addRow(self.inv_x)
        self.param_layout.addRow(self.inv_y)
        self.param_layout.addRow(self.inv_z)
        self.param_layout.addRow("刀具号:", self.tool_spin)
        self.param_layout.addRow("X偏移:", self.ox_spin)
        self.param_layout.addRow("Y偏移:", self.oy_spin)
        self.param_layout.addRow("Z偏移:", self.oz_spin)
        self.param_layout.addRow("查找:", self.find_edit)
        self.param_layout.addRow("替换为:", self.replace_edit)

        layout.addWidget(self.param_container)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.type_combo.currentIndexChanged.connect(self.on_type_changed)
        self.pos_combo.currentIndexChanged.connect(self.on_pos_changed)
        self.on_type_changed()

    def hide_all_params(self):
        for i in range(self.param_layout.rowCount()):
            label = self.param_layout.itemAt(i, QFormLayout.ItemRole.LabelRole)
            field = self.param_layout.itemAt(i, QFormLayout.ItemRole.FieldRole)
            if label and label.widget():
                label.widget().hide()
            if field and field.widget():
                field.widget().hide()

    def _show_param_widgets(self, widgets):
        for w in widgets:
            w.show()
            label = self.param_layout.labelForField(w)
            if label:
                label.show()

    def on_type_changed(self):
        self.hide_all_params()
        idx = self.type_combo.currentIndex()
        if idx == 0:  # 行文本编辑
            self._show_param_widgets(
                [self.pos_combo, self.custom_spin, self.text_edit, self.mode_combo]
            )
            self.on_pos_changed()
        elif idx == 1:  # 交换XY
            pass
        elif idx == 2:  # 坐标轴变换
            self._show_param_widgets(
                [self.dx_spin, self.dy_spin, self.dz_spin, self.inv_x, self.inv_y, self.inv_z]
            )
        elif idx == 3:  # 刀具偏移
            self._show_param_widgets(
                [self.tool_spin, self.ox_spin, self.oy_spin, self.oz_spin]
            )
        elif idx == 4:  # 文本替换
            self._show_param_widgets([self.find_edit, self.replace_edit])

    def on_pos_changed(self):
        show = (self.pos_combo.currentText() == "指定列")
        self.custom_spin.setVisible(show)
        label = self.param_layout.labelForField(self.custom_spin)
        if label:
            label.setVisible(show)

    def get_operation(self):
        idx = self.type_combo.currentIndex()
        if idx == 0:
            pos_map = {'行首':'start', '行尾':'end', '指定列':'custom'}
            position = pos_map[self.pos_combo.currentText()]
            custom_pos = self.custom_spin.value() if position == 'custom' else 0
            text = self.text_edit.text()
            mode = 'insert' if self.mode_combo.currentText() == '插入' else 'delete'
            desc = f"行文本编辑: {mode} '{text}' 于 {self.pos_combo.currentText()}"
            if position == 'custom':
                desc += f" (索引{custom_pos})"
            return Operation('edit_line_text', {'position': position, 'custom_pos': custom_pos, 'text': text, 'mode': mode}, desc)
        elif idx == 1:
            return Operation('swap_xy', {}, "交换 X 和 Y")
        elif idx == 2:
            params = {
                'dx': self.dx_spin.value(), 'dy': self.dy_spin.value(), 'dz': self.dz_spin.value(),
                'invert_x': self.inv_x.isChecked(), 'invert_y': self.inv_y.isChecked(), 'invert_z': self.inv_z.isChecked()
            }
            desc = f"坐标轴变换: 平移({params['dx']},{params['dy']},{params['dz']}) 反向(X:{params['invert_x']},Y:{params['invert_y']},Z:{params['invert_z']})"
            return Operation('transform', params, desc)
        elif idx == 3:
            params = {
                'tool': self.tool_spin.value(),
                'offset_x': self.ox_spin.value(), 'offset_y': self.oy_spin.value(), 'offset_z': self.oz_spin.value()
            }
            desc = f"刀具 T{params['tool']} 偏移 ({params['offset_x']},{params['offset_y']},{params['offset_z']})"
            return Operation('tool_offset', params, desc)
        elif idx == 4:
            find = self.find_edit.text()
            replace = self.replace_edit.text()
            return Operation('replace', {'find': find, 'replace': replace}, f"替换 '{find}' → '{replace}'")
        else:
            raise ValueError("未知操作类型")


# ====================== 批量处理线程 ======================
class BatchWorker(QThread):
    progress = pyqtSignal(int, int)
    file_done = pyqtSignal(str, bool, str)
    finished_all = pyqtSignal()

    def __init__(self, files, file_ops, template_ops):
        super().__init__()
        self.files = files
        self.file_ops = file_ops
        self.template_ops = template_ops

    def run(self):
        total = len(self.files)
        for i, path in enumerate(self.files):
            lines, ok, err = NCProcessor.load_file(path)
            if not ok:
                self.file_done.emit(path, False, err)
            else:
                ops = self.file_ops.get(path, []) or self.template_ops
                if not ops:
                    self.file_done.emit(path, False, '无操作可应用')
                else:
                    modified = NCProcessor.apply_operations(lines, ops)
                    ok, err = NCProcessor.save_file(path, modified)
                    self.file_done.emit(path, ok, err)
            self.progress.emit(i + 1, total)
        self.finished_all.emit()


# ====================== 主窗口 ======================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NC 文件修改器 - 完整功能版")
        self.setMinimumSize(1200, 800)

        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.itemSelectionChanged.connect(self.on_file_selected)

        self.file_operations = {}
        self.current_file = None
        self.orig_lines = []
        self.mod_lines = []

        self.init_ui()
        self.apply_style()
        self.statusBar().showMessage("就绪")

    def apply_style(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #f0f2f5; }
            QPushButton {
                background-color: #2c7da0; color: white; border: none;
                padding: 6px 12px; border-radius: 4px; font-weight: bold;
            }
            QPushButton:hover { background-color: #1f5e7a; }
            QPushButton#danger { background-color: #e76f51; }
            QPushButton#danger:hover { background-color: #d55c3a; }
            QPushButton#primary { background-color: #2a9d8f; }
            QPushButton#primary:hover { background-color: #21867a; }
            QListWidget, QPlainTextEdit { border: 1px solid #d0d7de; border-radius: 4px; background-color: white; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
                border: 1px solid #d0d7de; border-radius: 4px; padding: 4px;
                background-color: white;
            }
        """)

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(8)

        # ========== 上板块：水平分割 ==========
        top_splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：文件列表
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        btn_layout = QHBoxLayout()
        self.add_files_btn = QPushButton("添加文件")
        self.add_folder_btn = QPushButton("添加文件夹")
        self.clear_files_btn = QPushButton("清空列表")
        self.clear_files_btn.setObjectName("danger")
        self.select_all_btn = QPushButton("全选")
        btn_layout.addWidget(self.add_files_btn)
        btn_layout.addWidget(self.add_folder_btn)
        btn_layout.addWidget(self.select_all_btn)
        btn_layout.addWidget(self.clear_files_btn)
        left_layout.addLayout(btn_layout)

        left_layout.addWidget(QLabel("待处理文件列表（Ctrl+点击多选）:"))
        left_layout.addWidget(self.file_list)

        self.batch_btn = QPushButton("批量处理选中文件")
        self.batch_btn.setObjectName("primary")
        self.batch_btn.setToolTip(
            "对列表中选中的文件应用操作：优先用该文件自己的队列，"
            "若为空则使用当前预览文件的操作队列"
        )
        left_layout.addWidget(self.batch_btn)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_layout.addWidget(self.progress_bar)

        # 右侧：操作配置
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        op_btn_layout = QHBoxLayout()
        self.add_op_btn = QPushButton("添加操作")
        self.add_op_btn.setStyleSheet("background-color: #e9c46a; color: black;")
        self.clear_queue_btn = QPushButton("清空队列")
        self.clear_queue_btn.setObjectName("danger")
        self.clear_queue_btn.setToolTip("仅清空操作队列，预览恢复为未应用任何操作的状态")
        self.delete_op_btn = QPushButton("删除选中")
        self.delete_op_btn.setObjectName("danger")
        self.delete_op_btn.setToolTip("删除选中的操作记录，并撤销该步骤对预览的修改")
        op_btn_layout.addWidget(self.add_op_btn)
        op_btn_layout.addWidget(self.delete_op_btn)
        op_btn_layout.addWidget(self.clear_queue_btn)
        op_btn_layout.addStretch()
        right_layout.addLayout(op_btn_layout)

        right_layout.addWidget(QLabel("操作历史（点击预览该步，选中后可删除）:"))
        self.queue_list = QListWidget()
        self.queue_list.itemClicked.connect(self.on_queue_item_clicked)
        right_layout.addWidget(self.queue_list)

        top_splitter.addWidget(left_panel)
        top_splitter.addWidget(right_panel)
        top_splitter.setSizes([250, 350])

        # ========== 下板块：预览区域（带行号、同步滚动、精确高亮） ==========
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        ctrl_bar = QHBoxLayout()
        self.file_label = QLabel("未选择文件")
        self.file_label.setStyleSheet("font-weight: bold;")
        self.reset_btn = QPushButton("重置为原始")
        self.reset_btn.setToolTip("从磁盘重新加载文件，清空操作队列并放弃手动编辑")
        self.save_btn = QPushButton("保存 (Ctrl+S)")
        self.save_btn.setStyleSheet("background-color: #2a9d8f;")
        ctrl_bar.addWidget(QLabel("预览"))
        ctrl_bar.addWidget(self.file_label)
        ctrl_bar.addStretch()
        ctrl_bar.addWidget(self.reset_btn)
        ctrl_bar.addWidget(self.save_btn)
        preview_layout.addLayout(ctrl_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.orig_edit = SyncTextEdit()
        self.orig_edit.setReadOnly(True)
        self.orig_edit.setPlaceholderText("原始文件内容（只读）")
        self.orig_edit.setFont(QFont("Courier New", 10))
        self.mod_edit = SyncTextEdit()
        self.mod_edit.setPlaceholderText("修改后内容（可手动编辑）")
        self.mod_edit.setFont(QFont("Courier New", 10))
        self.mod_edit.textChanged.connect(self.on_manual_edit)
        self.orig_edit.set_sync_partner(self.mod_edit)
        self.mod_edit.set_sync_partner(self.orig_edit)

        splitter.addWidget(self.orig_edit)
        splitter.addWidget(self.mod_edit)
        splitter.setSizes([400, 600])
        preview_layout.addWidget(splitter, 1)

        # 整体垂直分割
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.addWidget(top_splitter)
        main_splitter.addWidget(preview_widget)
        main_splitter.setSizes([300, 500])
        main_layout.addWidget(main_splitter)

        # 信号
        self.add_files_btn.clicked.connect(self.add_files)
        self.add_folder_btn.clicked.connect(self.add_folder)
        self.select_all_btn.clicked.connect(self.select_all_files)
        self.clear_files_btn.clicked.connect(self.clear_files)
        self.batch_btn.clicked.connect(self.batch_process)
        self.add_op_btn.clicked.connect(self.show_add_operation)
        self.clear_queue_btn.clicked.connect(self.clear_queue)
        self.delete_op_btn.clicked.connect(self.delete_selected_operation)
        self.reset_btn.clicked.connect(self.reset_to_original)
        self.delete_op_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.queue_list)
        self.delete_op_shortcut.activated.connect(self.delete_selected_operation)
        self.save_btn.clicked.connect(self.save_current)

        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self.save_current)

    def show_add_operation(self):
        if not self.current_file:
            QMessageBox.warning(self, "警告", "请先在左侧选择一个文件")
            return
        dialog = OperationDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            op = dialog.get_operation()
            if self.current_file not in self.file_operations:
                self.file_operations[self.current_file] = []
            self.file_operations[self.current_file].append(op)
            self.refresh_queue()
            self.apply_operations_to_preview()

    def clear_queue(self):
        if self.current_file:
            self.file_operations[self.current_file] = []
            self.refresh_queue()
            self.apply_operations_to_preview()

    def delete_selected_operation(self):
        if not self.current_file:
            QMessageBox.warning(self, "警告", "请先在左侧选择一个文件")
            return
        row = self.queue_list.currentRow()
        if row < 0:
            QMessageBox.warning(self, "警告", "请先在操作历史中选中要删除的记录")
            return
        ops = self.file_operations.get(self.current_file, [])
        if row >= len(ops):
            return
        op = ops[row]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("确认删除")
        box.setText(
            f"确定删除操作「{op.description}」？\n该步骤对文件的修改将被撤销。"
        )
        yes_btn = box.addButton("确定", QMessageBox.ButtonRole.YesRole)
        no_btn = box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no_btn)
        box.exec()
        if box.clickedButton() != yes_btn:
            return
        del ops[row]
        self.refresh_queue()
        self.apply_operations_to_preview()
        if self.queue_list.count() > 0:
            self.queue_list.setCurrentRow(min(row, self.queue_list.count() - 1))
        self.statusBar().showMessage(f"已删除: {op.description}", 2000)

    def reset_to_original(self):
        if not self.current_file:
            QMessageBox.warning(self, "警告", "请先在左侧选择一个文件")
            return
        lines, ok, err = NCProcessor.load_file(self.current_file)
        if not ok:
            QMessageBox.critical(self, "加载失败", err)
            return
        self.orig_lines = lines
        self.file_operations[self.current_file] = []
        self.refresh_queue()
        self.mod_lines = lines.copy()
        self.update_preview_display()
        self.statusBar().showMessage("已从磁盘重新加载原始文件", 2000)

    def refresh_queue(self):
        self.queue_list.clear()
        if self.current_file and self.current_file in self.file_operations:
            for i, op in enumerate(self.file_operations[self.current_file], 1):
                self.queue_list.addItem(f"{i}. {op.description}")

    def apply_operations_to_preview(self):
        if not self.current_file:
            return
        ops = self.file_operations.get(self.current_file, [])
        self.mod_lines = NCProcessor.apply_operations(self.orig_lines, ops)
        self.update_preview_display()

    def update_preview_display(self):
        self.orig_edit.blockSignals(True)
        self.orig_edit.setPlainText(''.join(self.orig_lines))
        self.orig_edit.blockSignals(False)
        if not self.mod_lines:
            self.mod_edit.blockSignals(True)
            self.mod_edit.clear()
            self.mod_edit.blockSignals(False)
            return
        self.mod_edit.set_plain_text_with_diff(self.mod_lines, self.orig_lines)

    def on_manual_edit(self):
        plain = self.mod_edit.toPlainText()
        self.mod_lines = plain.splitlines(keepends=True)

    def save_current(self):
        if not self.current_file:
            QMessageBox.warning(self, "警告", "未选择文件")
            return
        ok, err = NCProcessor.save_file(self.current_file, self.mod_lines)
        if ok:
            self.orig_lines = self.mod_lines.copy()
            self.update_preview_display()
            self.statusBar().showMessage(f"已保存: {os.path.basename(self.current_file)}", 2000)
        else:
            QMessageBox.critical(self, "保存失败", err)

    def on_file_selected(self):
        items = self.file_list.selectedItems()
        if items:
            path = _norm_path(items[0].text())
            self.current_file = path
            self.file_label.setText(f"当前文件: {os.path.basename(path)}")
            lines, ok, err = NCProcessor.load_file(path)
            if ok:
                self.orig_lines = lines
                self.mod_lines = lines.copy()
                self.update_preview_display()
            else:
                self.orig_lines = []
                self.mod_lines = []
                self.update_preview_display()
            if path not in self.file_operations:
                self.file_operations[path] = []
            self.refresh_queue()
            self.apply_operations_to_preview()
        else:
            self.current_file = None
            self.file_label.setText("未选择文件")
            self.orig_lines = []
            self.mod_lines = []
            self.update_preview_display()
            self.queue_list.clear()

    def on_queue_item_clicked(self, item):
        idx = self.queue_list.row(item)
        ops = self.file_operations.get(self.current_file, [])
        if idx < len(ops):
            preview_ops = ops[:idx + 1]
            self.mod_lines = NCProcessor.apply_operations(self.orig_lines, preview_ops)
            self.update_preview_display()
            self.statusBar().showMessage(f"预览至第 {idx + 1} 步: {ops[idx].description}", 1500)

    def _list_paths(self):
        return [self.file_list.item(i).text() for i in range(self.file_list.count())]

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择NC文件", "", "NC Files (*.nc *.txt *.tap *.gcode);;All Files (*)"
        )
        existing = set(self._list_paths())
        for f in files:
            path = _norm_path(f)
            if path not in existing:
                self.file_list.addItem(path)
                existing.add(path)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            existing = set(self._list_paths())
            for ext in ['*.nc', '*.txt', '*.tap', '*.gcode']:
                for file in Path(folder).glob(ext):
                    path = _norm_path(str(file))
                    if path not in existing:
                        self.file_list.addItem(path)
                        existing.add(path)

    def select_all_files(self):
        self.file_list.selectAll()
        self.statusBar().showMessage(f"已全选 {self.file_list.count()} 个文件", 2000)

    def clear_files(self):
        self.file_list.clear()
        self.current_file = None
        self.orig_lines = []
        self.mod_lines = []
        self.update_preview_display()
        self.queue_list.clear()
        self.file_label.setText("未选择文件")
        self.file_operations.clear()

    def batch_process(self):
        selected = self.file_list.selectedItems()
        if not selected:
            QMessageBox.warning(
                self, "警告",
                "请先在列表中选中要处理的文件。\n"
                "提示：Ctrl+点击可多选，或点击「全选」后批量处理。"
            )
            return
        files = [item.text() for item in selected]
        template_ops = []
        if self.current_file:
            template_ops = self.file_operations.get(self.current_file, [])
        can_run = bool(template_ops) or any(
            self.file_operations.get(f, []) for f in files
        )
        if not can_run:
            QMessageBox.warning(
                self, "警告",
                "请先在当前预览文件中添加操作，再批量处理其他文件。\n"
                "（各文件若无单独队列，将使用当前文件的操作队列）"
            )
            return
        self.progress_bar.setVisible(True)
        self.progress_bar.setMaximum(len(files))
        self.progress_bar.setValue(0)
        self.batch_btn.setEnabled(False)
        self.add_op_btn.setEnabled(False)
        self.delete_op_btn.setEnabled(False)
        self.clear_queue_btn.setEnabled(False)

        self._batch_template_ops = template_ops
        self.batch_worker = BatchWorker(files, self.file_operations, template_ops)
        self.batch_worker.progress.connect(self.on_batch_progress)
        self.batch_worker.file_done.connect(self.on_batch_file_done)
        self.batch_worker.finished_all.connect(self.on_batch_finished)
        self.batch_worker.start()

    def on_batch_progress(self, cur, total):
        self.progress_bar.setValue(cur)
        self.statusBar().showMessage(f"批量处理: {cur}/{total}")

    def on_batch_file_done(self, path, success, msg):
        if not success:
            QMessageBox.warning(self, "错误", f"处理 {path} 失败: {msg}")

    def on_batch_finished(self):
        self.progress_bar.setVisible(False)
        self.batch_btn.setEnabled(True)
        self.add_op_btn.setEnabled(True)
        self.delete_op_btn.setEnabled(True)
        self.clear_queue_btn.setEnabled(True)
        template_ops = getattr(self, '_batch_template_ops', [])
        if template_ops:
            for i in range(self.file_list.count()):
                path = self.file_list.item(i).text()
                if not self.file_operations.get(path):
                    self.file_operations[path] = list(template_ops)
        self.statusBar().showMessage("批量处理完成", 3000)
        QMessageBox.information(self, "完成", "批量处理完成")
        if self.current_file:
            self.apply_operations_to_preview()


if __name__ == "__main__":
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        traceback.print_exception(exc_type, exc_value, exc_tb)
        input('程序异常，按回车键退出...')

    sys.excepthook = _excepthook
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())