import akshare as ak
import pandas as pd

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)
pd.set_option('display.max_rows', 80)

print("=" * 80)
print("1. Stock Daily History - 688411 (Hithium)")
print("=" * 80)
try:
    df = ak.stock_zh_a_hist(symbol="688411", period="daily", start_date="20260101", end_date="20260327", adjust="qfq")
    print(df.tail(30).to_string())
    print(f"\nTotal rows: {len(df)}")
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("2. Stock Individual Info - 688411")
print("=" * 80)
try:
    df_info = ak.stock_individual_info_em(symbol="688411")
    print(df_info.to_string())
except Exception as e:
    print(f"Error: {e}")

print("\n" + "=" * 80)
print("3. Stock Individual Fund Flow - 688411")
print("=" * 80)
try:
    df_fund = ak.stock_individual_fund_flow(stock="688411", market="sh")
    print(df_fund.tail(20).to_string())
except Exception as e:
    print(f"Error fund flow: {e}")

print("\n" + "=" * 80)
print("4. Crude Oil Futures (SC - INE)")
print("=" * 80)
try:
    df_oil = ak.futures_main_sina(symbol="SC0", start_date="20260101", end_date="20260327")
    print(df_oil.tail(20).to_string())
except Exception as e:
    print(f"Error oil: {e}")
    try:
        df_oil2 = ak.spot_hist_sge(symbol="SC2604")
        print(df_oil2.tail(10).to_string())
    except:
        print("Oil data not available via this method")
