import tushare as ts
import pandas as pd

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)
pd.set_option('display.max_rows', 60)

pro = ts.pro_api()

print("=" * 80)
print("1. Daily Price Data (Recent)")
print("=" * 80)
try:
    df = pro.daily(ts_code='688411.SH', start_date='20260101', end_date='20260327')
    if df is not None and len(df) > 0:
        print(df.head(30).to_string())
    else:
        print("No daily data returned")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("2. Daily Basic Indicators (PE/PB/MktCap)")
print("=" * 80)
try:
    df_basic = pro.daily_basic(ts_code='688411.SH', start_date='20260301', end_date='20260327')
    if df_basic is not None and len(df_basic) > 0:
        print(df_basic.to_string())
    else:
        print("No basic data returned")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("3. Money Flow")
print("=" * 80)
try:
    df_money = pro.moneyflow(ts_code='688411.SH', start_date='20260301', end_date='20260327')
    if df_money is not None and len(df_money) > 0:
        print(df_money.to_string())
    else:
        print("No money flow data returned")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("4. Financial Indicators")
print("=" * 80)
try:
    df_fina = pro.fina_indicator(ts_code='688411.SH')
    if df_fina is not None and len(df_fina) > 0:
        cols = ['ts_code', 'ann_date', 'end_date', 'eps', 'bps', 'roe', 'roa', 
                'grossprofit_margin', 'netprofit_margin', 'debt_to_assets',
                'revenue_ps', 'op_yoy', 'dt_netprofit_yoy']
        available_cols = [c for c in cols if c in df_fina.columns]
        print(df_fina[available_cols].head(8).to_string())
    else:
        print("No financial data returned")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("5. Company Basic Info")
print("=" * 80)
try:
    df_co = pro.stock_basic(ts_code='688411.SH')
    if df_co is not None and len(df_co) > 0:
        print(df_co.to_string())
    else:
        print("No company info returned")
except Exception as e:
    print(f"Error: {e}")
