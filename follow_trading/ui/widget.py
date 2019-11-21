from vnpy.event import EventEngine, Event
from vnpy.trader.engine import MainEngine
from vnpy.trader.ui import QtWidgets, QtCore, QtGui
from vnpy.trader.ui.widget import (
    BaseCell,
    MsgCell,
    TimeCell,
    BidCell,
    AskCell,
    BaseMonitor
)

from vnpy.trader.constant import (
    OrderType
)

from ..engine import APP_NAME, EVENT_FOLLOW_LOG, EVENT_FOLLOW_POS_DELTA


class ComboBox(QtWidgets.QComboBox):
    pop_show = QtCore.pyqtSignal()

    def showPopup(self):
        self.pop_show.emit()
        super(ComboBox, self).showPopup()


class FollowManager(QtWidgets.QWidget):
    signal_log = QtCore.pyqtSignal(Event)
    # timer = QtCore.QTimer()

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        """"""
        super(FollowManager, self).__init__()

        self.main_engine = main_engine
        self.event_engine = event_engine
        self.follow_engine = main_engine.get_engine(APP_NAME)

        self.sync_symbol = ''

        self.init_ui()
        self.follow_engine.init_engine()
        self.register_event()

    def init_ui(self):
        """"""
        self.setWindowTitle("跟随交易")
        self.setMinimumSize(1024, 768)
        self.setMaximumSize(1440, 900)

        # create widgets
        self.start_button = QtWidgets.QPushButton("启动")
        self.start_button.clicked.connect(self.start_follow)

        self.stop_button = QtWidgets.QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_follow)
        self.stop_button.setEnabled(False)

        self.sync_open_button = QtWidgets.QPushButton("单合约开仓同步")
        self.sync_open_button.clicked.connect(self.sync_open)

        self.sync_close_button = QtWidgets.QPushButton("单合约平仓同步")
        self.sync_close_button.clicked.connect(self.sync_close)

        self.sync_button = QtWidgets.QPushButton("单合约开平同步")
        self.sync_button.clicked.connect(self.sync_open_and_close)

        self.sync_all_button = QtWidgets.QPushButton("所有持仓同步")
        self.sync_all_button.clicked.connect(self.sync_all)

        for btn in [self.start_button,
                    self.stop_button,
                    self.sync_open_button,
                    self.sync_close_button,
                    self.sync_button,
                    self.sync_all_button]:
            btn.setFixedHeight(btn.sizeHint().height() * 2)

        gateways = self.follow_engine.get_connected_gateway_names()

        self.source_combo = ComboBox()
        self.source_combo.addItems(gateways)
        self.source_combo.pop_show.connect(self.refresh_gateway_name)
        self.target_combo = ComboBox()
        self.target_combo.addItems(gateways)
        self.target_combo.pop_show.connect(self.refresh_gateway_name)

        self.order_type_combo = QtWidgets.QComboBox()
        self.order_type_combo.addItems(['限价', '市价'])
        self.order_type_combo.activated[str].connect(self.set_order_type)

        self.follow_direction_combo = QtWidgets.QComboBox()
        self.follow_direction_combo.addItems(['正向跟随', '反向跟随'])

        self.sync_symbol_combo = ComboBox()
        self.sync_symbol_combo.pop_show.connect(self.refresh_symbol_list)
        self.sync_symbol_combo.activated[str].connect(self.set_sync_symbol)

        validator = QtGui.QIntValidator()
        self.timeout_line = QtWidgets.QLineEdit(str(self.follow_engine.cancel_order_timeout))
        self.timeout_line.setValidator(validator)
        self.timeout_line.editingFinished.connect(self.set_cancel_order_timeout)
        self.tickout_line = QtWidgets.QLineEdit(str(self.follow_engine.tick_add))
        self.tickout_line.setValidator(validator)
        self.tickout_line.editingFinished.connect(self.set_tick_add)
        self.multiples_line = QtWidgets.QLineEdit(str(self.follow_engine.multiples))
        self.multiples_line.setValidator(validator)
        self.multiples_line.editingFinished.connect(self.set_multiples)

        self.pos_delta_monitor = PosDeltaMonitor(self.main_engine, self.event_engine)
        self.log_monitor = LogMonitor(self.main_engine, self.event_engine)

        # set layout
        form = QtWidgets.QFormLayout()
        form.addRow("跟随接口名", self.source_combo)
        form.addRow("发单接口名", self.target_combo)
        form.addRow("发单类型", self.order_type_combo)
        form.addRow("跟单方向", self.follow_direction_combo)
        form.addRow("超时自动撤单（秒）", self.timeout_line)
        form.addRow("超价下单档位", self.tickout_line)
        form.addRow("跟随倍数", self.multiples_line)
        form.addRow(self.start_button)
        form.addRow(self.stop_button)

        form_sync = QtWidgets.QFormLayout()
        form_sync.addRow("同步合约", self.sync_symbol_combo)
        form_sync.addRow(self.sync_open_button)
        form_sync.addRow(self.sync_close_button)
        form_sync.addRow(self.sync_button)
        form_sync.addRow(self.sync_all_button)

        vbox = QtWidgets.QVBoxLayout()
        vbox.addLayout(form)
        vbox.addStretch()
        vbox.addLayout(form_sync)
        vbox.addStretch()

        grid = QtWidgets.QGridLayout()
        grid.addLayout(vbox, 0, 0, 2, 1)
        grid.addWidget(self.pos_delta_monitor, 0, 1)
        grid.addWidget(self.log_monitor, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 3)

        self.setLayout(grid)

    def register_event(self):
        """"""
        # self.timer.start(3000)
        # self.timer.timeout.connect(self.refresh_symbol_list)
        # self.timer.timeout.connect(self.test_timer2)
        # self.signal_log.connect(self.process_log_event)
        # self.event_engine.register(EVENT_FOLLOW_LOG, self.signal_log.emit)
        pass

    def set_sync_symbol(self, vt_symbol: str):
        """
        Set symbol to be synced
        """
        self.sync_symbol = vt_symbol
        self.write_log(f"选中合约{self.sync_symbol}")

    def set_order_type(self, order_type: str):
        """"""
        if order_type == "限价":
            self.follow_engine.set_parameters('order_type', OrderType.LIMIT)
        else:
            self.follow_engine.set_parameters('order_type', OrderType.MARKET)
        self.write_log(f"发单类型：{self.follow_engine.order_type.value} 切换成功")

    def set_cancel_order_timeout(self):
        """"""
        text = self.timeout_line.text()
        self.follow_engine.set_parameters('cancel_order_timeout', int(text))
        self.write_log(f"未成交自动撤单超时：{self.follow_engine.cancel_order_timeout} 秒设置成功")

    def set_tick_add(self):
        """"""
        text = self.tickout_line.text()
        self.follow_engine.set_parameters('tick_add', int(text))
        self.write_log(f"超价档位：{self.follow_engine.tick_add} 设置成功")

    def set_multiples(self):
        """"""
        text = self.multiples_line.text()
        self.follow_engine.set_parameters('multiples', int(text))
        self.write_log(f"跟随倍数：{self.follow_engine.multiples} 设置成功")

    def refresh_gateway_name(self):
        """"""
        gateways = self.follow_engine.get_connected_gateway_names()
        for combo in [self.source_combo, self.target_combo]:
            combo.clear()
            combo.addItems(gateways)
        self.write_log(f"接口名称获取成功")

    def refresh_symbol_list(self):
        """"""
        self.sync_symbol_combo.clear()
        symbol_list = list(self.follow_engine.get_positions().keys())
        self.sync_symbol_combo.addItems(symbol_list)

    def test_timer(self):
        """"""
        self.write_log("定时器测试")

    def test_timer2(self):
        """"""
        self.write_log("定时器多槽测试")

    def start_follow(self):
        """"""
        source = self.source_combo.currentText()
        target = self.target_combo.currentText()
        if source == target:
            self.follow_engine.write_log("跟随接口和发单接口不能是同一个")
            return
        self.follow_engine.set_gateways(source, target)

        follow_direction = self.follow_direction_combo.currentText()
        if follow_direction == '正向跟随':
            is_inverse = False
        else:
            is_inverse = True
        self.follow_engine.set_parameters('inverse_follow', is_inverse)

        order_type = self.order_type_combo.currentText()
        self.follow_engine.set_parameters('order_type', OrderType(order_type))

        self.follow_engine.set_parameters('multiples', int(self.multiples_line.text()))
        self.follow_engine.set_parameters('tick_add', int(self.tickout_line.text()))
        # self.follow_engine.set_parameters('filter_trade_timeout', int(self.timeout_line.text()))

        result = self.follow_engine.start()
        if result:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)

            self.source_combo.setEnabled(False)
            self.target_combo.setEnabled(False)
            self.follow_direction_combo.setEnabled(False)

    def stop_follow(self):
        """"""
        result = self.follow_engine.stop()
        if result:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)

    def validate_vt_symbol(self, vt_symbol: str):
        """"""
        if not vt_symbol:
            self.write_log(f"未选择要同步的合约")
            return
        vt_symbol = vt_symbol.strip()
        contract = self.main_engine.get_contract(vt_symbol)
        if not contract:
            self.write_log(f"尚未连接交易接口或合约{vt_symbol}不是可交易的合约")
        else:
            return vt_symbol

    def sync_open(self):
        """"""
        vt_symbol = self.sync_symbol
        if self.validate_vt_symbol(vt_symbol):
            self.follow_engine.sync_open_pos(vt_symbol)

    def sync_close(self):
        """"""
        vt_symbol = self.sync_symbol
        if self.validate_vt_symbol(vt_symbol):
            self.follow_engine.sync_close_pos(vt_symbol)

    def sync_open_and_close(self):
        """"""
        vt_symbol = self.sync_symbol
        if self.validate_vt_symbol(vt_symbol):
            self.follow_engine.sync_pos(vt_symbol)

    def sync_all(self):
        """"""
        self.follow_engine.sync_all_pos()

    def write_log(self, msg: str):
        """"""
        self.follow_engine.write_log(msg)

    def clear_log(self):
        """"""
        self.log_monitor.setRowCount(0)

    def show(self):
        """"""
        self.showNormal()


class PosDeltaMonitor(BaseMonitor):
    """
    Monitor for position delta.
    """
    event_type = EVENT_FOLLOW_POS_DELTA
    data_key = "vt_symbol"
    sorting = True

    headers = {
        "vt_symbol": {"display": "合约代码", "cell": BaseCell, "update": False},
        "source_long": {"display": "源账户多仓", "cell": BidCell, "update": True},
        "source_short": {"display": "源账户空仓", "cell": AskCell, "update": True},
        "target_long": {"display": "目标户多仓", "cell": BidCell, "update": True},
        "target_short": {"display": "目标户空仓", "cell": AskCell, "update": True},
        "long_delta": {"display": "多头仓差", "cell": BaseCell, "update": True},
        "short_delta": {"display": "空头仓差", "cell": BaseCell, "update": True},
    }


class LogMonitor(BaseMonitor):
    """
    Monitor for log data.
    """
    event_type = EVENT_FOLLOW_LOG
    data_key = ""
    sorting = False

    headers = {
        "time": {"display": "时间", "cell": TimeCell, "update": False},
        "msg": {"display": "信息", "cell": MsgCell, "update": False},
    }

    def init_ui(self):
        super(LogMonitor, self).init_ui()
        self.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)

    def insert_new_row(self, data):
        super(LogMonitor, self).insert_new_row(data)
        self.resizeRowToContents(0)