import pandas as pd
import numpy as np

def calculate_all_indicators(data):
    closes = data['Close']
    highs = data['High']
    lows = data['Low']
    
    return {
        'rsi': calculate_rsi(closes),
        'ma50': calculate_moving_average(closes, 50),
        'ma200': calculate_moving_average(closes, 200),
        'fib_levels': calculate_fib_levels(highs.max(), lows.min())
    }

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_moving_average(series, window):
    return series.rolling(window).mean()

def calculate_fib_levels(high, low):
    diff = high - low
    return {
        0.236: low + diff * 0.236,
        0.382: low + diff * 0.382,
        0.5: low + diff * 0.5,
        0.618: low + diff * 0.618,
        1.0: high
    }
