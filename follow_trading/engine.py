import pickle
import traceback
import pandas as pd

from collections import defaultdict
from datetime import datetime, timedelta, time
from enum import Enum
from copy import copy
from dataclasses import dataclass

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.utility import load_json, save_json, get_folder_path, get_file_path
from vnpy.trader.converter import OffsetConverter
from vnpy.trader.constant import (
    OrderType,
    Direction,
    Offset
)
from vnpy.trader.event import (
    EVENT_TICK,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_POSITION,
    EVENT_TIMER,
    EVENT_LOG
)
from vnpy.trader.object import (
    OrderRequest,
    SubscribeRequest,
    LogData,
    TickData,
    TradeData,
    OrderData,
    PositionData
)


@dataclass
class PosDeltaData:
    vt_symbol: str = ""
    source_long: int = 0
    source_short: int = 0
    source_net: int = 0
    target_long: int = 0
    target_short: int = 0
    target_net: int = 0
    long_delta: int = 0
    short_delta: int = 0
    net_delta: int = 0
    basic_delta: int = 0


class FollowRunType(Enum):
    TEST = "测试"
    LIVE = "实盘"


class TradeType(Enum):
    BUY = "买开"
    SHORT = "卖开"
    SELL = "卖平"
    COVER = "买平"


APP_NAME = "FollowTrading"
EVENT_FOLLOW_LOG = "eFollowLog"
EVENT_FOLLOW_POS_DELTA = "eFollowPosDelta"

DAYLIGHT_MARKET_END = time(15, 2)
NIGHT_MARKET_BEGIN = time(20, 45)


class FollowEngine(BaseEngine):
    """
    If following symbol is not intraday mode, The trade can follow many account send to 1 account.
    If following symbol is intraday mode. The trade can only one to one
    """
    setting_filename = "follow_trading_setting.json"
    data_filename = "follow_trading_data.json"

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine):
        super().__init__(main_engine, event_engine, APP_NAME)

        # Parameters
        self.source_gateway_name = "CTP"
        self.target_gateway_name = "RPC"
        self.filter_trade_timeout = 60
        self.cancel_order_timeout = 10
        self.multiples = 1
        self.tick_add = 10
        self.inverse_follow = False
        self.order_type = OrderType.LIMIT

        self.single_max = 1000
        self.intraday_symbols = ['IF', 'IC', 'IH']
        self.single_max_dict = {
            "IF": 20,
            "IC": 20,
            "IH": 20
        }

        # Test Mod
        self.run_type = FollowRunType.LIVE
        self.test_symbol = 'rb2001.SHFE'
        self.test_count = 0
        self.tick_time = None

        # Variables
        self.gateway_names = None
        self.is_active = False
        self.follow_data = {}
        self.follow_setting = {}

        self.sync_order_ref = 0
        self.tradeid_orderids_dict = {}  # vt_tradeid: vt_orderid
        self.positions = {}
        self.target_positions = {}

        self.vt_tradeids = set()
        self.limited_prices = {}
        self.latest_prices = {}
        self.due_out_req_list = []
        self.refresh_pos_interval = 0

        self.is_hedged_closed = False

        self.is_trade_saved = False

        # Timeout auto cancel
        self.active_order_set = set()
        self.active_order_counter = {}

        self.offset_converter = OffsetConverter(main_engine)

        # If parameter is python object. It can not convert to json directly
        self.parameters = ['source_gateway_name', 'target_gateway_name', 'filter_trade_timeout',
                           'cancel_order_timeout', 'multiples', 'tick_add', 'inverse_follow',
                           'order_type', 'run_type',
                           'test_symbol', 'intraday_symbols',
                           'single_max',
                           'single_max_dict']

        self.variables = ['tradeid_orderids_dict', 'positions']
        self.clear_variables = ['tradeid_orderids_dict']
        self.pos_key = ['source_long', 'source_short', 'source_net',
                        'target_long', 'target_short', 'target_net', 'net_delta', 'basic_delta']

        self.skip_contracts = []

        self.load_data()

    def init_engine(self):
        """
        Init engine.
        """
        self.write_log("参数和数据读取成功。")
        # update vt_tradeid firstly
        self.update_tradeids()
        print('vt_tradeids', self.vt_tradeids)

        self.register_event()
        if self.run_type == FollowRunType.TEST:
            self.write_log("测试模式：订阅行情以获取最新时间。")
            self.subscribe(self.test_symbol)
        else:
            self.write_log("实盘模式：定期校时以确保时间准确。")
        self.write_log("跟随交易初始化完成。")

    def load_data(self):
        """
        Load variables and settings
        """
        self.load_follow_setting()
        self.load_follow_data()

    def get_current_time(self):
        """
        Get current time when market is opening. If market closed please use datetime.now()
        """
        if self.run_type == FollowRunType.LIVE:
            if self.tick_time is None:
                now = datetime.now()
            else:
                if datetime.now() - self.tick_time > timedelta(seconds=self.filter_trade_timeout):
                    self.write_log("系统时间超过行情时间1分钟，可能是收市或行情中断，开始使用系统时间")
                    now = datetime.now()
                else:
                    now = self.tick_time
        else:
            now = datetime.now()
        return now

    def set_gateways(self, source_name: str, target_name: str):
        """
        Set gateway names.
        """
        self.source_gateway_name = source_name
        self.target_gateway_name = target_name

    def set_parameters(self, param_name, value):
        """"""
        setattr(self, param_name, value)

    def get_pos(self, vt_symbol: str, name: str):
        """"""
        symbol_pos = self.positions.get(vt_symbol, None)
        if symbol_pos:
            return symbol_pos[name]

    def set_pos(self, vt_symbol: str, name: str, pos: int):
        """"""
        symbol_pos = self.positions.get(vt_symbol, None)
        if symbol_pos:
            symbol_pos[name] = pos

    def get_connected_gateway_names(self):
        """
        Get connected gateway names.
        """
        # if not self.gateway_names:
        accounts = self.main_engine.get_all_accounts()
        print(accounts)
        self.gateway_names = [account.gateway_name for account in accounts]
        print(self.gateway_names)
        return self.gateway_names

    def get_positions(self):
        """"""
        return self.positions

    def get_skip_contracts(self):
        """"""
        return self.skip_contracts

    def get_intraday_symbols(self):
        """"""
        return self.intraday_symbols

    def load_follow_setting(self):
        """
        Load setting from setting file.
        """
        self.follow_setting = load_json(self.setting_filename)
        print(self.follow_setting)
        for name in self.parameters:
            value = self.follow_setting.get(name, None)
            if value:
                if name == 'order_type':
                    setattr(self, name, OrderType(value))
                elif name == 'run_type':
                    setattr(self, name, FollowRunType(value))
                else:
                    setattr(self, name, value)
        self.write_log("参数配置读取成功")

    def save_follow_setting(self):
        """
        Save follow setting to setting file.
        """
        for name in self.parameters:
            if name in ['order_type', 'run_type']:
                self.follow_setting[name] = getattr(self, name).value
            else:
                self.follow_setting[name] = getattr(self, name)
        save_json(self.setting_filename, self.follow_setting)
        self.write_log("参数配置存储成功")

    def load_follow_data(self):
        """
        Load run data from data file.
        """
        self.follow_data = load_json(self.data_filename)
        for name in self.variables:
            value = self.follow_data.get(name, None)
            if value:
                setattr(self, name, value)
        self.write_log("运行数据读取成功")

    def save_follow_data(self):
        """
        Save run data to data file.
        """
        for name in self.variables:
            self.follow_data[name] = getattr(self, name)
        save_json(self.data_filename, self.follow_data)

    def clear_follow_data(self):
        """
        Clear follow data after market closed
        """
        if self.follow_data:
            # save to history data file
            today = datetime.now().strftime('%Y%m%d')
            save_json(f"follow_history/{today}_{self.data_filename}", self.follow_data)
            self.write_log("清除临时数据并保存至历史成功")

            # clear the template variables
            for name in self.clear_variables:
                self.follow_data[name].clear()
            save_json(self.data_filename, self.follow_data)

    def save_trade(self):
        """
        Save trade record to file.
        """
        today = datetime.now().strftime('%Y%m%d')
        trade_folder = get_folder_path('trade')
        trade_file_path = trade_folder.joinpath(f"trade_{today}.csv")

        account_id = 'null'
        accounts = self.main_engine.get_all_accounts()
        for account in accounts:
            if account.gateway_name == self.source_gateway_name:
                account_id = account.accountid
                break

        trades = self.main_engine.get_all_trades()
        trade_list = []
        for trade in trades:
            d = copy(trade.__dict__)
            d["exchange"] = d["exchange"].value
            d["direction"] = d["direction"].value
            d["offset"] = d["offset"].value
            d['dt'] = f"{today} {d['time']}"
            d['date'] = f"{today}"
            d['source_account'] = account_id
            d.pop("vt_symbol")

            trade_list.append(d)
        df = pd.DataFrame(trade_list)
        df.to_csv(trade_file_path, index=False, encoding='utf-8')
        self.write_log("成交记录保存成功")

    def save_account_info(self):
        """
        Save account info to file every day
        """
        today = datetime.now().strftime('%Y%m%d')
        account_file = get_file_path("account_info.csv")

        account_text = ""
        accounts = self.main_engine.get_all_accounts()
        for account in accounts:
            txt_ = f"{today},{account.accountid},{account.balance},{account.available}\n"
            account_text += txt_
        with open(account_file, "a+", encoding="utf-8") as f:
            f.write(account_text)
        self.write_log("账户信息保存成功")

    def update_tradeids(self):
        """
        Update received tradeids from main engine
        """
        trades = self.main_engine.get_all_trades()
        tradeids = [trade.vt_tradeid for trade in trades]
        self.vt_tradeids.update(set(tradeids))
        self.write_log("成交单列表更新成功")

    def auto_save_trade(self):
        """
        Auto saved sorts of info after market closed.
        """
        if self.is_trade_saved:
            return

        now_time = datetime.now().time()
        if NIGHT_MARKET_BEGIN > now_time >= DAYLIGHT_MARKET_END:
            self.save_trade()
            self.clear_follow_data()
            self.save_account_info()

            self.is_trade_saved = True

    def start(self):
        """
        Start follow trading.
        """
        if self.is_active:
            self.write_log("跟随交易运行中")
            return False

        if self.source_gateway_name == self.target_gateway_name:
            self.write_log("跟随接口和发单接口不能是同一个")
            return False

        self.is_active = True
        self.write_log("跟随交易启动")

        return True

    def stop(self):
        """
        Stop follow trading.
        """
        if not self.is_active:
            self.write_log("跟随交易尚未启动")
            return False

        self.is_active = False
        self.cancel_all_order()
        self.write_log("跟随交易停止")

        self.clear_empty_pos()
        self.clear_expired_pos()

        self.save_follow_setting()
        # self.save_follow_data()

        self.save_trade()
        self.save_contract()

        now_time = datetime.now().time()
        if NIGHT_MARKET_BEGIN > now_time >= DAYLIGHT_MARKET_END:
            self.clear_follow_data()
            self.save_account_info()
        return True

    def close(self):
        """
        Close engine.
        """
        self.stop()

    def save_contract(self):
        """"""
        contracts = self.main_engine.get_all_contracts()
        filepath = get_file_path('contracts.data')
        with open(filepath, 'wb') as f:
            pickle.dump(contracts, f)
        self.write_log(f"当日合约数据保存成功")

    @staticmethod
    def get_trade_type(trade: TradeData):
        """
        Convert trade type to buy/sell/short/cover
        """
        if trade.direction == Direction.LONG:
            if trade.offset == Offset.OPEN:
                return TradeType.BUY
            else:
                return TradeType.COVER
        else:
            if trade.offset == Offset.OPEN:
                return TradeType.SHORT
            else:
                return TradeType.SELL

    @staticmethod
    def inverse_req(req: OrderRequest):
        """Inverse trade"""
        req.direction = Direction.SHORT if req.direction == Direction.LONG else Direction.LONG
        return req

    @staticmethod
    def strip_digit(symbol: str):
        """"""
        res = ""
        for char in symbol:
            if not char.isdigit():
                res += char
            else:
                break
        return res


    def is_intra_day_symbol(self, symbol: str):
        """"""
        return self.strip_digit(symbol) in self.intraday_symbols

    def split_req(self, req: OrderRequest):
        """Split order if needed"""
        symbol = self.strip_digit(req.symbol)
        symbol_single_max = self.single_max_dict.get(symbol, self.single_max)
        order_max = min(symbol_single_max, self.single_max)

        if req.volume <= order_max:
            return [req]

        max_count, remainder = divmod(req.volume, order_max)

        req_max = copy(req)
        req_max.volume = order_max
        req_list = [req_max for i in range(int(max_count))]

        if remainder:
            req_r = copy(req)
            req_r.volume = remainder
            req_list.append(req_r)
        return req_list

    def register_event(self):
        """"""
        self.event_engine.register(EVENT_TICK, self.process_tick_event)
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_TRADE, self.process_trade_event)
        self.event_engine.register(EVENT_POSITION, self.process_position_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def process_tick_event(self, event: Event):
        """"""
        tick = event.data
        self.tick_time = tick.datetime
        self.init_limited_price(tick)
        self.update_latest_price(tick)

    def process_order_event(self, event: Event):
        """
        process order from target gateway.
        """
        try:
            order = event.data
            vt_orderid = order.vt_orderid
            if order.gateway_name == self.source_gateway_name:
                return

            self.offset_converter.update_order(order)

            # Filter non-follow order
            if not self.filter_target_not_follow(order.vt_orderid):
                return

            if order.is_active():
                self.active_order_set.add(vt_orderid)
                self.active_order_counter[vt_orderid] = 0
            else:
                if vt_orderid in self.active_order_set:
                    self.active_order_counter.pop(vt_orderid)
                    self.active_order_set.remove(vt_orderid)
        except:  # noqa
            msg = f"处理委托事件，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def process_trade_event(self, event: Event):
        """"""
        try:
            trade = event.data

            # Filter duplicate trade push if reconnect gateway for disconnected reason.
            if trade.vt_tradeid in self.vt_tradeids:
                self.write_log(f"{trade.vt_tradeid}是重复推送。")
                return
            else:
                self.vt_tradeids.add(trade.vt_tradeid)

            if not self.is_active:
                self.write_log(f"{trade.vt_tradeid}不跟随，系统尚未启动。")
                return

            if trade.gateway_name == self.source_gateway_name:
                # validate source trade
                if not self.filter_source_trade(trade):
                    return

                # generate order request based on trade
                req = self.convert_trade_to_order_req(trade)
                if not req:
                    return

                # send orders or push to order cache
                self.send_order(req, trade.vt_tradeid)
            else:
                self.offset_converter.update_trade(trade)
                if not self.filter_target_not_follow(trade.vt_orderid):
                    self.write_log(f"{trade.vt_tradeid} 不是跟随策略的成交单。")
                    return
                self.update_target_pos(trade)
        except:  # noqa
            msg = f"处理成交事件，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def process_timer_event(self, event: Event):
        """"""
        try:
            self.send_queue_order()
            self.cancel_timeout_order()
            # self.view_test_variables()
            self.refresh_pos()
            self.auto_save_trade()
        except:  # noqa
            msg = f"处理定时事件，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def process_position_event(self, event: Event):
        """
        update source gateway position and target gateway offset converter position
        """
        try:
            position = event.data

            if self.is_active:
                self.pre_subscribe(position)
            if position.gateway_name == self.source_gateway_name:
                self.update_source_pos(position)
            else:
                self.offset_converter.update_position(position)
        except:  # noqa
            msg = f"处理持仓事件，触发异常：\n{traceback.format_exc()}"
            self.write_log(msg)

    def pre_subscribe(self, position: PositionData):
        """
        Pre subscribe symbol in source gateway position to speed up following.
        """
        vt_symbol = position.vt_symbol
        if not self.is_price_inited(vt_symbol):
            if self.subscribe(vt_symbol):
                self.write_log(f"{vt_symbol}行情订阅请求已发送")

    def cancel_timeout_order(self):
        """
        Cancel active order if timeout exceed specified value.
        """
        for vt_orderid, counter in self.active_order_counter.items():
            print("counter:", vt_orderid, counter)
            if counter is None:
                continue

            if counter > self.cancel_order_timeout:
                self.cancel_order(vt_orderid)
                self.active_order_counter[vt_orderid] = 0
                self.write_log(f"委托单{vt_orderid} 超过最大等待时间，已执行撤单。")

            self.active_order_counter[vt_orderid] += 1

    def refresh_pos(self):
        """
        Put pos delta event regularly.
        """
        if self.refresh_pos_interval > 3:
            for vt_symbol in self.positions:
                # print('refresh', vt_symbol)
                self.put_pos_delta_event(vt_symbol)
            self.refresh_pos_interval = 0
        self.refresh_pos_interval += 1

    def view_pos(self):
        """
        For Test used
        """
        print('=' * 100)
        print('Symbol Position:')
        print('-' * 100)
        for symbol, pos_d in self.positions.items():
            print(symbol, end="\t|\t")
            for k, v in pos_d.items():
                print(k, v, end='\t')
            print('')
            print('-' * 100)

    def view_test_variables(self):
        """
        For Test used to view variables.
        """
        if self.test_count > 5:
            self.view_pos()
            print('vt_tradeids', self.vt_tradeids)
            self.test_count = 0
        self.test_count += 1

    def clear_empty_pos(self):
        """
        Clear empty pos data after stop engine.
        """
        for symbol in list(self.positions.keys()):
            pos = self.positions[symbol]
            if (pos['source_long'] + pos['source_short'] + pos['target_long'] + pos['target_short'] == 0):
                self.positions.pop(symbol)

    def clear_expired_pos(self):
        """
        clear expired contract data
        """
        for symbol in list(self.positions.keys()):
            contract = self.main_engine.get_contract(symbol)
            if not contract:
                self.positions.pop(symbol)
                self.write_log(f"{symbol}已过期，清除成功。")


    def init_symbol_pos(self, vt_symbol: str):
        """
        Create symbol pos dict.
        """
        self.positions[vt_symbol] = {}
        for pos_key in self.pos_key:
            self.positions[vt_symbol][pos_key] = 0

    def update_source_pos(self, position: PositionData):
        """
        Update source gateway pos.
        """
        if position.direction == Direction.NET:
            return

        vt_symbol = position.vt_symbol
        if self.positions.get(vt_symbol, None) is None:
            self.init_symbol_pos(vt_symbol)
        else:
            symbol_pos = self.positions[vt_symbol]
            if position.direction == Direction.LONG:
                symbol_pos['source_long'] = position.volume
            else:
                symbol_pos['source_short'] = position.volume

            symbol_pos['source_net'] = symbol_pos['source_long'] - symbol_pos['source_short']
            symbol_pos['net_delta'] = symbol_pos['source_net'] * self.multiples - symbol_pos['target_net']

    def update_target_pos(self, trade: TradeData):
        """
        Update pos in target gateway
        """
        vt_symbol = trade.vt_symbol
        if self.positions.get(vt_symbol, None) is None:
            self.init_symbol_pos(vt_symbol)

        symbol_pos = self.positions[vt_symbol]
        trade_type = self.get_trade_type(trade)
        if trade_type == TradeType.BUY:
            symbol_pos['target_long'] += trade.volume
        elif trade_type == TradeType.SHORT:
            symbol_pos['target_short'] += trade.volume
        elif trade_type == TradeType.SELL:
            symbol_pos['target_long'] -= trade.volume
        else:
            symbol_pos['target_short'] -= trade.volume
        symbol_pos['target_net'] = symbol_pos['target_long'] - symbol_pos['target_short']
        symbol_pos['net_delta'] = symbol_pos['source_net'] * self.multiples - symbol_pos['target_net']

        self.save_follow_data()
        self.put_pos_delta_event(vt_symbol)
        self.write_log(f"{vt_symbol}仓位更新成功")

    def subscribe(self, vt_symbol: str):
        """
        Subscribe to get latest price and limit price.
        """
        contract = self.main_engine.get_contract(vt_symbol)
        if contract:
            req = SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange)
            self.main_engine.subscribe(req, self.source_gateway_name)
            return True

    def init_limited_price(self, tick: TickData):
        """
        Save symbol limit-up and limit-down price.
        """
        vt_symbol = tick.vt_symbol
        if vt_symbol not in self.limited_prices:
            d = {
                'limit_up': tick.limit_up,
                'limit_down': tick.limit_down
            }
            self.limited_prices[vt_symbol] = d

    def update_latest_price(self, tick: TickData):
        """
        Update symbol bid-1 price and ask-1 price.
        """
        vt_symbol = tick.vt_symbol
        if self.latest_prices.get(vt_symbol, None) is None:
            self.latest_prices[vt_symbol] = {}
        self.latest_prices[vt_symbol]['bid_price'] = tick.bid_price_1
        self.latest_prices[vt_symbol]['ask_price'] = tick.ask_price_1

    def is_timeout_trade(self, trade: TradeData):
        """
        If trade happened a specified period of time before now, it usually happened if take a long time to reconnect.
        Because trade is not in self.vt_tradeids(if app don't restart). so it can't be filtered by self.vt_tradeids
        """
        now = self.get_current_time()
        trade_time = datetime.strptime(trade.time, '%H:%M:%S')
        trade_time = trade_time.replace(year=now.year, month=now.month, day=now.day)
        if now - trade_time > timedelta(seconds=self.filter_trade_timeout):
            self.write_log(f"{trade.vt_tradeid} 成交时间：{trade.time} 超过跟单有效期。")
            return True
        else:
            return False

    def is_followed_trade(self, trade: TradeData):
        """"""
        if trade.vt_tradeid in self.tradeid_orderids_dict:
            self.write_log(f"{trade.vt_tradeid} 已跟随，无需重复跟随。")
            return True
        else:
            return False

    def is_skip_contract_trade(self, trade: TradeData):
        if trade.vt_symbol in self.skip_contracts:
            self.write_log(f"{trade.vt_tradeid} 合约{trade.vt_symbol}禁止同步。")
            return True
        else:
            return False

    def filter_source_trade(self, trade: TradeData):
        """
        Filter trade from source gateway.
        """
        # filter skip contract
        if self.is_skip_contract_trade(trade):
            return

        # filter timeout trade
        if self.is_timeout_trade(trade):
            return

        # filter followed trade push when restart app
        if self.is_followed_trade(trade):
            return

        return trade

    def filter_target_not_follow(self, vt_orderid: str):
        """"""
        for sub_list in self.tradeid_orderids_dict.values():
            for orderid in sub_list:
                if vt_orderid == orderid:
                    return True

    def validate_target_pos(self, req: OrderRequest):
        """
        Validate symbol pos in target gateway.
        """
        vt_symbol = req.vt_symbol
        symbol_pos = self.positions.get(vt_symbol, None)
        if symbol_pos is None:
            self.write_log(f"{vt_symbol} 跟随策略该品种的仓位不存在。")
            return
        else:
            if req.direction == Direction.LONG:
                short_pos = symbol_pos['target_short']
                if short_pos <= 0:
                    self.write_log(f"{vt_symbol} 跟随策略该品种空头仓位不足。")
                    return
                else:
                    req.volume = min(req.volume, short_pos)
            else:
                long_pos = symbol_pos['target_long']
                if long_pos <= 0:
                    self.write_log(f"{vt_symbol} 跟随策略该品种多头仓位不足。")
                    return
                else:
                    req.volume = min(req.volume, long_pos)
            return req

    def convert_order_price(
        self,
        vt_symbol: str,
        direction: Direction,
        price: float = 0
    ):
        """
        Make sure price is in limit-up and limit-down range.
        """
        # call this function only self.is_price_inited() is True.
        limit_price = self.limited_prices.get(vt_symbol)
        latest_prices = self.latest_prices.get(vt_symbol)

        # if limit up or limt down happend, save ask or bid price to variable.
        # do not directly use self.latest_prices, because it restore to the big number when tick updated.
        ask_price = min(latest_prices['ask_price'], limit_price['limit_up'])
        if latest_prices['bid_price'] > limit_price['limit_up']:
            bid_price = limit_price['limit_down']
        else:
            bid_price = latest_prices['bid_price']

        contract = self.main_engine.get_contract(vt_symbol)
        if direction == Direction.LONG:
            price = ask_price if not price else price
            # If market price type or market price in manual order (when price is set to -1)
            if self.order_type == OrderType.MARKET or price == -1:
                price = limit_price['limit_up']
            else:
                price = min(limit_price['limit_up'], price + self.tick_add * contract.pricetick)
        else:
            price = bid_price if not price else price
            if self.order_type == OrderType.MARKET or price == -1:
                price = limit_price['limit_down']
            else:
                price = max(limit_price['limit_down'], price - self.tick_add * contract.pricetick)

        return price

    def convert_trade_to_order_req(self, trade: TradeData):
        """
        Trade convert to order request
        """
        if trade.offset == Offset.NONE:
            self.write_log(f"{trade.vt_tradeid} offset为None，非CTP正常成交单。")
            return
        if trade.direction == Direction.NET:
            self.write_log(f"{trade.vt_tradeid} direction为Net， 非CTP正常成交单。")
            return

        req = OrderRequest(
            symbol=trade.symbol,
            exchange=trade.exchange,
            direction=trade.direction,
            type=OrderType.LIMIT,
            volume=trade.volume,
            price=trade.price,
            offset=trade.offset
        )
        req.volume = req.volume * self.multiples

        if self.inverse_follow:
            req = self.inverse_req(req)

        # T0 symbol use lock mode, redirect.
        if self.strip_digit(trade.vt_symbol) in self.intraday_symbols:
            return req

        # Normal mode, check position if close
        if trade.offset != Offset.OPEN:
            req.offset = Offset.CLOSE
            return self.validate_target_pos(req)
        else:
            return req

    def is_price_inited(self, vt_symbol: str):
        """
        Check if limited price and latest price ready.
        """
        if self.limited_prices.get(vt_symbol, None) and self.latest_prices.get(vt_symbol, None):
            return True
        else:
            return False

    def send_order(
        self,
        req: OrderRequest,
        vt_tradeid: str
    ):
        """
        Send order to order queue.
        """
        if not self.is_price_inited(req.vt_symbol):
            self.subscribe(req.vt_symbol)
            self.write_log(f"{req.vt_symbol}订阅请求已发送。")
        self.due_out_req_list.append((vt_tradeid, req))
        self.write_log(f"{vt_tradeid}核验通过，已进入发单队列")

    def send_queue_order(self):
        """
        Send order in queue after limited price is ready.
        """
        if not self.due_out_req_list:
            return

        for req_tuple in copy(self.due_out_req_list):
            vt_tradeid, req = req_tuple
            if not self.is_price_inited(req.vt_symbol):
                print('Limit unready in send queue order event')
                continue

            self.send_and_record(req, vt_tradeid)
            self.due_out_req_list.remove(req_tuple)

    def send_and_record(
        self,
        req: OrderRequest,
        vt_tradeid: str
    ):
        """
        Send and record result.
        """
        req.price = self.convert_order_price(req.vt_symbol, req.direction, req.price)
        vt_orderids = self.convert_and_send_orders(req)
        if vt_orderids:
            self.tradeid_orderids_dict[vt_tradeid] = vt_orderids
            if vt_tradeid.startswith('SYNC'):
                order_prefix = "同步单"
            elif vt_tradeid.startswith('BASIC'):
                order_prefix = "底仓单"
            else:
                order_prefix = "跟随单"

            self.write_log(f"{order_prefix} {vt_tradeid}发单成功，委托号：{'  '.join(vt_orderids)}。")

            # Save data to file
            self.save_follow_data()
            # self.write_log(f"{order_prefix} {vt_tradeid}记录保存成功。")
        return vt_orderids

    def convert_and_send_orders(self, req: OrderRequest):
        """
        Convert a req to req list and send order to gateway.
        """
        lock = True if self.strip_digit(req.vt_symbol) in self.intraday_symbols else False

        req_list = self.offset_converter.convert_order_request(req, lock=lock)
        if not req_list:
            self.write_log("委托单转换模块转换失败，可能是目标账户实际可用仓位不足。")
            return

        vt_orderids = []
        for req in req_list:
            # split req
            splited_req_list = self.split_req(req)
            for splited_req in splited_req_list:
                vt_orderid = self.main_engine.send_order(splited_req, self.target_gateway_name)
                if not vt_orderid:
                    continue
                vt_orderids.append(vt_orderid)
                self.offset_converter.update_order_request(splited_req, vt_orderid)

        return vt_orderids

    def cancel_order(self, vt_orderid: str):
        """
        Cancel existing order by vt_orderid.
        """
        order = self.main_engine.get_order(vt_orderid)
        if not order:
            self.write_log(f"撤单失败，找不到委托号 {vt_orderid}。")
            return

        req = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)
        self.write_log(f"委托号{vt_orderid}撤单请求已报。")

    def cancel_all_order(self, vt_symbol: str = ""):
        """
        Cancel all active orders or orders of vt_symbol in target gateway
        """
        active_orders = self.main_engine.get_all_active_orders(vt_symbol)

        target_orders = [order for order in active_orders if order.gateway_name == self.target_gateway_name]
        for order in target_orders:
            self.cancel_order(order.vt_orderid)

    def is_pos_exists(self, vt_symbol: str):
        """
        If pos of vt_symbol in positions
        """
        symbol_pos = self.positions.get(vt_symbol, None)
        if symbol_pos is None:
            self.write_log(f"{vt_symbol}仓位不存在。")
            return False
        else:
            return True

    def get_pos_delta(self, vt_symbol: str):
        """
        Calculate pos delta between source gateway and target gateway. make sure vt_symbol is existed.
        """
        # calculate pos delta
        symbol_pos = self.positions.get(vt_symbol, None)

        if not self.inverse_follow:
            long_pos_delta = symbol_pos['source_long'] * self.multiples - symbol_pos['target_long']
            short_pos_delta = symbol_pos['source_short'] * self.multiples - symbol_pos['target_short']
        else:
            long_pos_delta = symbol_pos['source_short'] * self.multiples - symbol_pos['target_long']
            short_pos_delta = symbol_pos['source_long'] * self.multiples - symbol_pos['target_short']

        return long_pos_delta, short_pos_delta

    def get_net_pos_delta(self, vt_symbol: str):
        """
        Calculate net pos. If not sync basic position, it need adjust by basic pos.
        """
        symbol_pos = self.positions.get(vt_symbol, None)
        delta = symbol_pos['source_net'] * self.multiples - symbol_pos['target_net']
        net_pos_delta = delta if not self.inverse_follow else (- delta)
        return net_pos_delta

    def sync_net_pos_delta(self, vt_symbol: str, is_sync_basic: bool = False):
        """
        If contract is intra-day mode. Only can sync by net pos.
        """
        if self.strip_digit(vt_symbol) in self.intraday_symbols:
            symbol_pos = self.positions.get(vt_symbol, None)
            net_pos_delta = self.get_net_pos_delta(vt_symbol)
            if not is_sync_basic:
                net_pos_delta = net_pos_delta - symbol_pos['basic_delta']

            market_price = True if is_sync_basic else False
            if net_pos_delta > 0:
                self.buy(vt_symbol, net_pos_delta, market_price=market_price, is_basic=is_sync_basic)
            elif net_pos_delta < 0:
                self.short(vt_symbol, abs(net_pos_delta), market_price=market_price, is_basic=is_sync_basic)
            else:
                self.write_log(f"{vt_symbol}净仓差与底仓差一致，仓差不是跟随交易引起的，无需同步。")
                return
            
            if is_sync_basic:
                symbol_pos = self.positions.get(vt_symbol, None)
                symbol_pos['basic_delta'] = 0
        else:
            self.write_log(f"{vt_symbol}不是日内模式。")


    def sync_open_pos(self, vt_symbol: str):
        """"""
        if self.strip_digit(vt_symbol) in self.intraday_symbols:
            self.write_log(f"{vt_symbol}是日内模式，只支持同步净仓。")
            return

        if self.is_pos_exists(vt_symbol):
            # cancel order first
            self.cancel_all_order(vt_symbol)

            long_pos_delta, short_pos_delta = self.get_pos_delta(vt_symbol)
            if long_pos_delta > 0:
                self.buy(vt_symbol, long_pos_delta)
            else:
                self.write_log(f"多开仓同步：{vt_symbol}目标户无仓差或多仓更多，无需同步。")

            if short_pos_delta > 0:
                self.short(vt_symbol, short_pos_delta)
            else:
                self.write_log(f"空开仓同步：{vt_symbol}目标户无仓差或空仓更多，无需同步。")

    def sync_close_pos(self, vt_symbol: str):
        """"""
        if self.strip_digit(vt_symbol) in self.intraday_symbols:
            self.write_log(f"{vt_symbol}是日内模式，只支持同步净仓。")
            return

        if self.is_pos_exists(vt_symbol):
            # cancel order first
            self.cancel_all_order(vt_symbol)

            long_pos_delta, short_pos_delta = self.get_pos_delta(vt_symbol)
            if long_pos_delta < 0:
                self.sell(vt_symbol, abs(long_pos_delta))
            else:
                self.write_log(f"多平仓同步：{vt_symbol}目标户无仓差或多仓更少，无需同步。")

            if short_pos_delta < 0:
                self.cover(vt_symbol, abs(short_pos_delta))
            else:
                self.write_log(f"空平仓同步：{vt_symbol}目标户无仓差或空仓更少，无需同步。")

    def sync_pos(self, vt_symbol: str):
        """Sync position between source and target by vt_symbol"""
        if self.is_pos_exists(vt_symbol):
            long_pos_delta, short_pos_delta = self.get_pos_delta(vt_symbol)
            if long_pos_delta == short_pos_delta == 0:
                self.write_log(f"{vt_symbol}源账户与目标户仓位一致，无需同步。")
                return

            self.sync_open_pos(vt_symbol)
            self.sync_close_pos(vt_symbol)

    def sync_all_pos(self):
        """Sync pos of all non-empty contract"""
        for vt_symbol in list(self.positions.keys()):
            self.sync_pos(vt_symbol)

    def send_sync_order_req(
        self,
        vt_symbol: str,
        direction: Direction,
        volume: int,
        price: float,
        offset: Offset,
        market_price: bool,
        is_basic: bool
    ):
        """
        Create order request for sync pos.
        """
        # print(vt_symbol, direction, volume, price, offset, market_price, is_basic)

        if not self.is_active:
            self.write_log("跟随系统尚未启动，不能同步。")

        contract = self.main_engine.get_contract(vt_symbol)
        req = OrderRequest(
            symbol=contract.symbol,
            exchange=contract.exchange,
            direction=direction,
            type=OrderType.LIMIT,
            volume=volume,
            price=price,
            offset=offset,
        )

        if market_price:
            req.price = -1

        sync_flag = "BASIC" if is_basic else "SYNC"

        # trade_id is required or it will be filtered, then pos can't be calculated correctly.
        now_time = self.get_current_time()
        time_id = f"{now_time.strftime('%H%M%S')}{str(now_time.microsecond // 1000)}"
        self.sync_order_ref += 1
        vt_tradeid = f"{sync_flag}_{time_id}_{self.sync_order_ref}"
        self.send_order(req, vt_tradeid)

    def buy(
        self,
        vt_symbol: str,
        volume: int,
        price: float = 0,
        market_price: bool = False,
        is_basic: bool = False
    ):
        """"""
        self.send_sync_order_req(vt_symbol, Direction.LONG, volume, price, Offset.OPEN, market_price, is_basic)

    def short(
        self,
        vt_symbol: str,
        volume: int,
        price: float = 0,
        market_price: bool = False,
        is_basic: bool = False
    ):
        """"""
        self.send_sync_order_req(vt_symbol, Direction.SHORT, volume, price, Offset.OPEN, market_price, is_basic)

    def sell(
        self,
        vt_symbol: str,
        volume: int,
        price: float = 0,
        market_price: bool = False,
        is_basic: bool = False
    ):
        """"""
        self.send_sync_order_req(vt_symbol, Direction.SHORT, volume, price, Offset.CLOSE, market_price, is_basic)

    def cover(
        self,
        vt_symbol: str,
        volume: int,
        price: float = 0,
        market_price: bool = False,
        is_basic: bool = False
    ):
        """"""
        self.send_sync_order_req(vt_symbol, Direction.LONG, volume, price, Offset.CLOSE, market_price, is_basic)


    def close_hedged_pos(self, vt_symbol: str, pos: int):
        """
        Close hedged pos.
        """
        symbol_pos = self.positions.get(vt_symbol, None)
        if symbol_pos:
            avaiable = min(symbol_pos['target_long'], symbol_pos['target_short'])
            if pos <= avaiable:
                self.sell(vt_symbol, pos, market_price=True)
                self.cover(vt_symbol, pos, market_price=True)
                self.write_log(f"已对冲仓位平仓委托单已报，{vt_symbol}，手数：{pos}。")
            else:
                self.write_log(f"平仓手数超出最大已对冲仓位。")


    def put_pos_delta_event(self, vt_symbol: str):
        """
        Calculate delta pos and put event
        """
        pos_dict = self.positions.get(vt_symbol, None)
        if pos_dict:
            pos_dict['target_net'] = pos_dict['target_long'] - pos_dict['target_short']
            pos_dict = copy(pos_dict)
            pos_dict['vt_symbol'] = vt_symbol
            pos_dict['long_delta'], pos_dict['short_delta'] = self.get_pos_delta(vt_symbol)
            pos_dict['net_delta'] = self.get_net_pos_delta(vt_symbol)

            pos_data = PosDeltaData()
            pos_data.__dict__ = pos_dict
            event = Event(EVENT_FOLLOW_POS_DELTA, pos_data)
            self.event_engine.put(event)

    def write_log(self, msg: str):
        """"""
        log = LogData(msg=msg, gateway_name=APP_NAME)
        event = Event(EVENT_FOLLOW_LOG, log)
        self.event_engine.put(event)
