import requests
import time
import asyncio
import aiohttp
import json
import threading
from datetime import datetime
import platform
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox, filedialog
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.dates as mdates
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import urllib3

# Импорт WebSocket с проверкой
try:
    import websocket
    # Проверяем, что это правильная библиотека websocket-client
    if hasattr(websocket, 'WebSocketApp'):
        HAS_WEBSOCKET = True
    else:
        HAS_WEBSOCKET = False
        print("❌ Неправильная версия websocket. Нужна websocket-client")
except ImportError:
    HAS_WEBSOCKET = False
    print("❌ Не установлен websocket-client")

# Исправляем проблему с aiodns на Windows
if platform.system() == 'Windows':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Отключаем предупреждения SSL для скорости
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =============================================================================
# ДВОЙНОЙ WebSocket СПРЕД МОНИТОР MEXC + GATE
# =============================================================================

class MexcWebSocketClient:
    def __init__(self, symbol: str, callback=None):
        self.symbol = symbol.upper() + "_USDT"
        self.callback = callback
        self.ws = None
        self.ws_thread = None
        self.running = False
        
        # Текущие лучшие цены
        self.best_bid = None
        self.best_ask = None
        self.last_update = 0
        
    def on_message(self, ws, message):
        """Обработка сообщений от WebSocket"""
        try:
            # Проверяем, что сообщение не пустое
            if not message or not isinstance(message, str):
                return
                
            data = json.loads(message)
            
            # Проверяем, что data это словарь
            if not isinstance(data, dict):
                return
            
            # Отладочная информация
            if data.get('channel') == 'pong':
                return
            
            # Обрабатываем данные тикера
            if data.get('channel') == 'push.ticker' and data.get('symbol') == self.symbol:
                ticker_data = data.get('data', {})
                if isinstance(ticker_data, dict):
                    bid = ticker_data.get('bid1')
                    ask = ticker_data.get('ask1')
                    
                    if bid and ask:
                        try:
                            self.best_bid = float(bid)
                            self.best_ask = float(ask)
                            self.last_update = time.time()
                            
                            if self.callback:
                                self.callback(self.best_bid, self.best_ask)
                        except (ValueError, TypeError):
                            pass
                    
        except json.JSONDecodeError:
            # Игнорируем сообщения, которые не являются JSON
            pass
        except Exception as e:
            print(f"Ошибка обработки WebSocket сообщения MEXC: {e}")
            print(f"Сообщение: {str(message)[:200]}...")
    
    def on_error(self, ws, error):
        """Обработка ошибок WebSocket"""
        print(f"WebSocket ошибка MEXC: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Обработка закрытия WebSocket"""
        print("WebSocket соединение MEXC закрыто")
        if self.running:
            time.sleep(2)
            self.connect()
    
    def on_open(self, ws):
        """Обработка открытия WebSocket"""
        print(f"WebSocket MEXC подключен для {self.symbol}")
        
        # Подписываемся на тикер
        subscribe_message = {
            "method": "sub.ticker",
            "param": {
                "symbol": self.symbol
            }
        }
        ws.send(json.dumps(subscribe_message))
        
        # Запускаем ping каждые 20 секунд
        def ping_loop():
            while self.running:
                try:
                    if ws.sock and ws.sock.connected:
                        ping_message = {"method": "ping"}
                        ws.send(json.dumps(ping_message))
                    time.sleep(20)
                except:
                    break
        
        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()
    
    def connect(self):
        """Подключение к WebSocket"""
        if not self.running or not HAS_WEBSOCKET:
            return
            
        try:
            self.ws = websocket.WebSocketApp(
                "wss://contract.mexc.com/edge",
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open
            )
            
            self.ws.run_forever()
        except Exception as e:
            print(f"Ошибка подключения WebSocket MEXC: {e}")
            if self.running:
                time.sleep(5)
                self.connect()
    
    def start(self):
        """Запуск WebSocket клиента"""
        self.running = True
        self.ws_thread = threading.Thread(target=self.connect, daemon=True)
        self.ws_thread.start()
        print(f"Запускаем WebSocket MEXC для {self.symbol}")
    
    def stop(self):
        """Остановка WebSocket клиента"""
        self.running = False
        if self.ws:
            self.ws.close()
        print(f"Остановили WebSocket MEXC для {self.symbol}")


class GateWebSocketClient:
    def __init__(self, symbol: str, callback=None):
        self.symbol = symbol.upper() + "_USDT"
        self.callback = callback
        self.ws = None
        self.ws_thread = None
        self.running = False
        
        # Текущие лучшие цены
        self.best_bid = None
        self.best_ask = None
        self.last_update = 0
        
    def on_message(self, ws, message):
        """Обработка сообщений от WebSocket"""
        try:
            # Проверяем, что сообщение не пустое
            if not message or not isinstance(message, str):
                return
            
            data = json.loads(message)
            
            # Проверяем, что data это словарь
            if not isinstance(data, dict):
                return
            
            # Проверяем результат подписки
            result = data.get('result')
            if isinstance(result, dict) and result.get('status') == 'success':
                print(f"Gate WebSocket: успешная подписка на {self.symbol}")
                return
            
            # Проверяем ответ на ping
            if data.get('result') == 'pong':
                return
            
            # Обрабатываем обновления глубины рынка
            if data.get('method') == 'depth.update':
                params = data.get('params', [])
                if len(params) >= 3:
                    clean = params[0]  # True если полные данные
                    depth_data = params[1]
                    market = params[2]
                    
                    if market == self.symbol and isinstance(depth_data, dict):
                        asks = depth_data.get('asks', [])
                        bids = depth_data.get('bids', [])
                        
                        if asks and bids and len(asks[0]) >= 2 and len(bids[0]) >= 2:
                            self.best_ask = float(asks[0][0])  # Первый ask
                            self.best_bid = float(bids[0][0])  # Первый bid
                            self.last_update = time.time()
                            
                            if self.callback:
                                self.callback(self.best_bid, self.best_ask)
                    
        except json.JSONDecodeError:
            # Игнорируем сообщения, которые не являются JSON
            pass
        except Exception as e:
            print(f"Ошибка обработки WebSocket сообщения Gate: {e}")
            print(f"Сообщение: {str(message)[:200]}...")
    
    def on_error(self, ws, error):
        """Обработка ошибок WebSocket"""
        print(f"WebSocket ошибка Gate: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        """Обработка закрытия WebSocket"""
        print("WebSocket соединение Gate закрыто")
        if self.running:
            time.sleep(2)
            self.connect()
    
    def on_open(self, ws):
        """Обработка открытия WebSocket"""
        print(f"WebSocket Gate подключен для {self.symbol}")
        
        # Подписываемся на глубину рынка
        subscribe_message = {
            "id": 12312,
            "method": "depth.subscribe",
            "params": [self.symbol, 5, "0"]  # market, limit, interval
        }
        ws.send(json.dumps(subscribe_message))
        
        # Запускаем ping каждые 30 секунд
        def ping_loop():
            while self.running:
                try:
                    if ws.sock and ws.sock.connected:
                        ping_message = {"id": 9999, "method": "server.ping", "params": []}
                        ws.send(json.dumps(ping_message))
                    time.sleep(30)
                except:
                    break
        
        ping_thread = threading.Thread(target=ping_loop, daemon=True)
        ping_thread.start()
    
    def connect(self):
        """Подключение к WebSocket"""
        if not self.running or not HAS_WEBSOCKET:
            return
            
        try:
            self.ws = websocket.WebSocketApp(
                "wss://ws.gate.io/v3/",
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open
            )
            
            self.ws.run_forever()
        except Exception as e:
            print(f"Ошибка подключения WebSocket Gate: {e}")
            if self.running:
                time.sleep(5)
                self.connect()
    
    def start(self):
        """Запуск WebSocket клиента"""
        self.running = True
        self.ws_thread = threading.Thread(target=self.connect, daemon=True)
        self.ws_thread.start()
        print(f"Запускаем WebSocket Gate для {self.symbol}")
    
    def stop(self):
        """Остановка WebSocket клиента"""
        self.running = False
        if self.ws:
            self.ws.close()
        print(f"Остановили WebSocket Gate для {self.symbol}")


class PriceMonitor:
    def __init__(self, ticker: str, gui_callback=None):
        self.ticker = ticker.upper()
        self.mexc_symbol = f"{self.ticker}_USDT"
        self.gate_symbol = f"{self.ticker}_USDT"
        
        # WebSocket клиенты
        self.mexc_ws_client = None
        self.gate_ws_client = None
        
        # Данные стаканов
        self.mexc_best_bid = None
        self.mexc_best_ask = None
        self.gate_best_bid = None
        self.gate_best_ask = None
        
        # Данные для графика с фиксированным временным окном
        self.start_time = datetime.now()
        self.spread_history = deque(maxlen=1000000)  # 300 точек ≈ 5 минут
        self.time_history = deque(maxlen=1000000)
        
        # Callback для обновления GUI
        self.gui_callback = gui_callback
        
        # REST fallback сессия
        self.gate_session = requests.Session()
        self._setup_rest_session()
        
    def _setup_rest_session(self):
        """Настраивает сессию requests для fallback"""
        self.gate_session.verify = False
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=1
        )
        self.gate_session.mount('http://', adapter)
        self.gate_session.mount('https://', adapter)
    
    def mexc_ws_callback(self, bid, ask):
        """Callback для обновления данных MEXC через WebSocket"""
        self.mexc_best_bid = bid
        self.mexc_best_ask = ask
    
    def gate_ws_callback(self, bid, ask):
        """Callback для обновления данных Gate через WebSocket"""
        self.gate_best_bid = bid
        self.gate_best_ask = ask
    
    def start_websockets(self):
        """Запуск WebSocket для обеих бирж"""
        if not HAS_WEBSOCKET:
            print("❌ WebSocket не доступен, используется fallback к REST API")
            return False
        
        # Запускаем MEXC WebSocket
        self.mexc_ws_client = MexcWebSocketClient(self.ticker, self.mexc_ws_callback)
        self.mexc_ws_client.start()
        
        # Запускаем Gate WebSocket
        self.gate_ws_client = GateWebSocketClient(self.ticker, self.gate_ws_callback)
        self.gate_ws_client.start()
        
        # Даем время на подключение
        time.sleep(3)
        return True
    
    def get_mexc_orderbook_rest_fallback(self):
        """Fallback к REST API для MEXC"""
        try:
            mexc_futures_symbol = f"{self.ticker}_USDT"
            url = f'https://contract.mexc.com/api/v1/contract/depth/{mexc_futures_symbol}'
            
            response = self.gate_session.get(url, timeout=3)
            
            if response.status_code == 200:
                data = response.json()
                
                if 'success' in data and data.get('success') and 'data' in data:
                    order_data = data['data']
                else:
                    order_data = data
                
                if order_data.get('bids') and order_data.get('asks'):
                    self.mexc_best_bid = float(order_data['bids'][0][0])
                    self.mexc_best_ask = float(order_data['asks'][0][0])
                    return True
                    
        except Exception as e:
            print(f"Ошибка получения данных MEXC REST: {e}")
        return False
    
    def get_gate_orderbook_rest_fallback(self):
        """Fallback к REST API для Gate"""
        try:
            url = f'https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={self.gate_symbol}&limit=5'
            
            response = self.gate_session.get(url, timeout=3)
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('bids') and data.get('asks'):
                    self.gate_best_bid = float(data['bids'][0]['p'])
                    self.gate_best_ask = float(data['asks'][0]['p'])
                    return True
                    
        except Exception as e:
            print(f"Ошибка получения данных Gate REST: {e}")
        return False
    
    def update_data(self):
        """Обновление данных"""
        # Если WebSocket недоступен, используем REST fallback
        if not HAS_WEBSOCKET or not self.mexc_ws_client or not self.gate_ws_client:
            self.get_mexc_orderbook_rest_fallback()
            self.get_gate_orderbook_rest_fallback()
    
    def calculate_spread(self):
        """Вычисляет арбитражный спред между биржами"""
        if (self.mexc_best_bid is None or self.mexc_best_ask is None or 
            self.gate_best_bid is None or self.gate_best_ask is None):
            return None
        
        mexc_mid = (self.mexc_best_bid + self.mexc_best_ask) / 2
        gate_mid = (self.gate_best_bid + self.gate_best_ask) / 2
        
        if mexc_mid > gate_mid:
            mexc_price = self.mexc_best_bid
            gate_price = self.gate_best_ask
            direction = "MEXC выше (продаем на MEXC, покупаем на Gate)"
            spread_percent = ((mexc_price - gate_price) / gate_price) * 100
        else:
            mexc_price = self.mexc_best_ask
            gate_price = self.gate_best_bid
            direction = "Gate выше (покупаем на MEXC, продаем на Gate)"
            spread_percent = ((gate_price - mexc_price) / mexc_price) * 100
        
        result = {
            'mexc_price': mexc_price,
            'gate_price': gate_price,
            'spread_percent': spread_percent,
            'direction': direction
        }
        return result
    
    def update_history(self, spread_percent: float):
        """Обновляет историю данных для графика"""
        current_time = datetime.now()
        time_diff = (current_time - self.start_time).total_seconds()
        
        self.time_history.append(time_diff)
        self.spread_history.append(spread_percent)
    
    def print_spread(self):
        """Выводит информацию о спреде и обновляет GUI"""
        spread_data = self.calculate_spread()
        
        if spread_data:
            current_time = datetime.now().strftime("%H:%M:%S")
            mode_indicator = "" if HAS_WEBSOCKET and self.mexc_ws_client and self.gate_ws_client else "(REST)"
            log_message = f"{current_time:>8} MEXC:{spread_data['mexc_price']:>12.8f} Gate:{spread_data['gate_price']:>12.8f} Спред:{spread_data['spread_percent']:>8.2f}% {mode_indicator}"
            
            self.update_history(spread_data['spread_percent'])
            
            if self.gui_callback:
                self.gui_callback(log_message, spread_data)
            else:
                print(log_message)
            return True
        return False
    
    def cleanup(self):
        """Очищает ресурсы"""
        if self.mexc_ws_client:
            self.mexc_ws_client.stop()
        if self.gate_ws_client:
            self.gate_ws_client.stop()
        if hasattr(self, 'gate_session'):
            self.gate_session.close()


class SpreadMonitorGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("⚡ MEXC + GATE Двойной WebSocket Спред Монитор")
        self.root.geometry("1200x800")
        
        self.monitor = None
        self.monitoring = False
        self.update_job = None
        
        self.setup_gui()
        
    def setup_gui(self):
        """Настройка интерфейса"""
        # Верхняя панель управления
        control_frame = tk.Frame(self.root)
        control_frame.pack(pady=10, padx=10, fill=tk.X)
        
        # Ввод тикера
        tk.Label(control_frame, text="Тикер:").pack(side=tk.LEFT)
        self.ticker_entry = tk.Entry(control_frame, width=10)
        self.ticker_entry.pack(side=tk.LEFT, padx=5)
        self.ticker_entry.insert(0, "BTC")
        
        # Кнопки управления
        self.start_button = tk.Button(control_frame, text="🚀 Старт (WS×2)", 
                                     command=self.start_monitoring,
                                     bg="#4CAF50", fg="white", font=("Arial", 10, "bold"))
        self.start_button.pack(side=tk.LEFT, padx=5)
        
        self.stop_button = tk.Button(control_frame, text="⏹ Стоп", 
                                    command=self.stop_monitoring,
                                    bg="#f44336", fg="white", font=("Arial", 10, "bold"),
                                    state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)
        
        self.clear_button = tk.Button(control_frame, text="🗑 Очистить", 
                                     command=self.clear_data,
                                     bg="#ff9800", fg="white", font=("Arial", 10, "bold"))
        self.clear_button.pack(side=tk.LEFT, padx=5)
        
        self.save_button = tk.Button(control_frame, text="💾 Сохранить", 
                                    command=self.save_chart,
                                    bg="#2196F3", fg="white", font=("Arial", 10, "bold"))
        self.save_button.pack(side=tk.LEFT, padx=5)
        
        # Статус
        self.status_label = tk.Label(control_frame, text="⚡ Готов (MEXC + Gate через WebSocket)", 
                                    fg="green", font=("Arial", 10, "bold"))
        self.status_label.pack(side=tk.RIGHT, padx=10)
        
        # Основной фрейм
        main_frame = tk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Левая панель - лог
        left_frame = tk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        tk.Label(left_frame, text="📊 Лог спреда (MEXC WS + Gate WS):", 
                font=("Arial", 10, "bold")).pack(pady=5)
        
        self.log_text = scrolledtext.ScrolledText(left_frame, height=25, width=50, 
                                                 font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # Правая панель - график
        right_frame = tk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        # Создаем matplotlib figure
        self.fig, self.ax = plt.subplots(figsize=(8, 6))
        self.fig.tight_layout(pad=3.0)
        
        # Интегрируем matplotlib в tkinter
        self.canvas = FigureCanvasTkAgg(self.fig, master=right_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        # Инициализируем пустой график
        self.ax.set_xlabel('Время (мм:сс)')
        self.ax.set_ylabel('Спред (%)')
        self.ax.set_title('⚡ MEXC (WS) vs GATE (WS)')
        self.ax.grid(True, alpha=0.3)
        self.ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
        self.canvas.draw()
        
    def format_time_axis(self, x, pos):
        """Форматирует ось времени в минуты:секунды"""
        minutes = int(x // 60)
        seconds = int(x % 60)
        return f"{minutes}:{seconds:02d}"
        
    def update_gui(self, log_message: str, spread_data: dict):
        """Обновление GUI"""
        try:
            # Обновляем лог
            self.log_text.insert(tk.END, log_message + "\n")
            self.log_text.see(tk.END)
            
            # Обновляем график каждые 2 точки для плавности
            if self.monitor and len(self.monitor.time_history) > 1:
                if len(self.monitor.time_history) % 2 == 0:
                    self._update_chart()
                    
        except Exception:
            pass
    
    def _update_chart(self):
        """Обновление графика с фиксированным временным окном"""
        try:
            self.ax.clear()
            
            time_data = list(self.monitor.time_history)
            spread_data_points = list(self.monitor.spread_history)
            
            if len(time_data) > 0 and len(spread_data_points) > 0:
                # Рисуем график с цветовой кодировкой
                for i, (t, s) in enumerate(zip(time_data, spread_data_points)):
                    color = '#00C851' if s >= 0 else '#FF4444'
                    if i > 0:
                        prev_t, prev_s = time_data[i-1], spread_data_points[i-1]
                        self.ax.plot([prev_t, t], [prev_s, s], color=color, linewidth=2.5)
                    self.ax.plot(t, s, 'o', color=color, markersize=4)
                
                # Настраиваем оси
                self.ax.set_xlabel('Время (мм:сс)')
                self.ax.set_ylabel('Спред (%)')
                self.ax.set_title(f'⚡ {self.monitor.ticker}/USDT (WS×2) (+ MEXC выше, - Gate выше)')
                
                # Фиксированное временное окно: показываем последние 5 минут
                current_time = max(time_data)
                window_start = max(0, current_time - 1000000)
                window_end = current_time + 30  # Небольшой отступ справа
                
                self.ax.set_xlim(window_start, window_end)
                
                # Форматируем ось времени
                self.ax.xaxis.set_major_formatter(plt.FuncFormatter(self.format_time_axis))
                plt.setp(self.ax.xaxis.get_majorticklabels(), rotation=45)
            
            self.ax.grid(True, alpha=0.3)
            self.ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            self.canvas.draw()
        except Exception:
            pass
    
    def save_chart(self):
        """Сохраняет график"""
        try:
            ticker = self.ticker_entry.get().strip().upper()
            if not ticker:
                messagebox.showerror("Ошибка", "Введите тикер")
                return
            
            if not self.monitor or len(self.monitor.time_history) == 0:
                messagebox.showwarning("Предупреждение", "Нет данных для сохранения")
                return
            
            import os
            downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(downloads_path, f"{ticker}_DoubleWebSocket_{timestamp}.png")
            
            save_fig, save_ax = plt.subplots(figsize=(12, 8))
            save_fig.patch.set_facecolor('white')
            
            time_data = list(self.monitor.time_history)
            spread_data_points = list(self.monitor.spread_history)
            
            for i, (t, s) in enumerate(zip(time_data, spread_data_points)):
                color = '#00C851' if s >= 0 else '#FF4444'
                if i > 0:
                    prev_t, prev_s = time_data[i-1], spread_data_points[i-1]
                    save_ax.plot([prev_t, t], [prev_s, s], color=color, linewidth=2)
                save_ax.plot(t, s, 'o', color=color, markersize=3)
            
            save_ax.set_xlabel('Время (мм:сс)', fontsize=12)
            save_ax.set_ylabel('Спред (%)', fontsize=12)
            save_ax.set_title(f'⚡ {ticker}/USDT (MEXC + Gate WebSocket)', fontsize=14, fontweight='bold')
            save_ax.grid(True, alpha=0.3)
            save_ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
            
            save_ax.xaxis.set_major_formatter(plt.FuncFormatter(self.format_time_axis))
            plt.setp(save_ax.xaxis.get_majorticklabels(), rotation=45)
            
            save_fig.tight_layout()
            save_fig.savefig(filename, dpi=1000000, bbox_inches='tight', 
                           facecolor='white', edgecolor='none')
            plt.close(save_fig)
            
            messagebox.showinfo("Успех", f"График сохранен:\n{filename}")
            
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{str(e)}")
    
    def monitoring_cycle(self):
        """Цикл мониторинга"""
        if not self.monitoring:
            return
            
        try:
            self.monitor.update_data()
            self.monitor.print_spread()
            
        except Exception:
            pass
        
        # Планируем следующее обновление каждые 500ms для WebSocket режима
        if self.monitoring:
            interval = 500 if HAS_WEBSOCKET else 1000
            self.update_job = self.root.after(interval, self.monitoring_cycle)
    
    def start_monitoring(self):
        """Запуск мониторинга"""
        ticker = self.ticker_entry.get().strip()
        if not ticker:
            self.status_label.config(text="❌ Ошибка: введите тикер", fg="red")
            return
        
        if self.monitoring:
            return
            
        self.monitoring = True
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.status_label.config(text=f"🚀 Двойной WS {ticker}...", fg="blue")
        
        # Очищаем интерфейс
        self.log_text.delete("1.0", tk.END)
        self.ax.clear()
        self.ax.set_xlabel('Время (мм:сс)')
        self.ax.set_ylabel('Спред (%)')
        self.ax.set_title(f'⚡ {ticker}/USDT (WebSocket×2)')
        self.ax.grid(True, alpha=0.3)
        self.ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
        self.canvas.draw()
        
        # Создаем монитор
        self.monitor = PriceMonitor(ticker, self.update_gui)
        
        # Запускаем WebSocket для обеих бирж
        ws_started = self.monitor.start_websockets()
        
        if not ws_started:
            self.status_label.config(text=f"🚀 REST режим {ticker}...", fg="orange")
        else:
            self.status_label.config(text=f"⚡ Двойной WS {ticker} активен", fg="green")
        
        # Начинаем мониторинг
        self.monitoring_cycle()
    
    def stop_monitoring(self):
        """Остановка мониторинга"""
        self.monitoring = False
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.status_label.config(text="⏹ Остановлен", fg="red")
        
        if self.update_job:
            self.root.after_cancel(self.update_job)
            self.update_job = None
            
        if self.monitor:
            self.monitor.cleanup()
    
    def clear_data(self):
        """Очистка данных"""
        self.log_text.delete("1.0", tk.END)
        self.ax.clear()
        self.ax.set_xlabel('Время (мм:сс)')
        self.ax.set_ylabel('Спред (%)')
        self.ax.set_title('⚡ MEXC (WS) vs GATE (WS)')
        self.ax.grid(True, alpha=0.3)
        self.ax.axhline(y=0, color='gray', linestyle='--', alpha=0.7)
        self.canvas.draw()
        
        if self.monitor:
            self.monitor.spread_history.clear()
            self.monitor.time_history.clear()
            self.monitor.start_time = datetime.now()
    
    def run(self):
        """Запуск GUI"""
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.mainloop()
    
    def on_closing(self):
        """Обработка закрытия"""
        self.stop_monitoring()
        if self.monitor:
            self.monitor.cleanup()
        self.root.destroy()


if __name__ == "__main__":
    print("=" * 70)
    print("⚡ ДВОЙНОЙ WebSocket СПРЕД МОНИТОР MEXC + GATE ⚡")
    print("=" * 70)
    print()
    
    # Проверяем WebSocket
    if HAS_WEBSOCKET:
        print("✅ WebSocket поддержка активна")
        print("🚀 MEXC через WebSocket (реальное время)")
        print("🚀 Gate через WebSocket (реальное время)")
        print("⚡ Максимальная скорость обновления данных!")
    else:
        print("⚠️  WebSocket НЕ доступен - используется REST режим")
        print("🔧 Для WebSocket выполните:")
        print("   pip uninstall websocket websockets")
        print("   pip install websocket-client")
        print("✅ Fallback на REST API для обеих бирж")
    
    print("✅ Фиксированное временное окно (5 минут)")
    print("✅ Цветовая индикация направления спреда")
    print("✅ Автоматическое переподключение при разрыве")
    print()
    
    # Запускаем GUI
    gui = SpreadMonitorGUI()
    gui.run()