"""
股票筛选程序
使用 Tushare API 获取数据，根据特定的量价关系筛选股票
"""

import tushare as ts
import pandas as pd
import configparser
import os
from datetime import datetime, timedelta
import time
from tqdm import tqdm


class StockSelector:
    def __init__(self, config_file='config.ini'):
        """初始化选股器"""
        # 读取配置文件
        config = configparser.ConfigParser()
        
        # 检查配置文件是否存在
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"配置文件 {config_file} 不存在！")
        
        # 尝试不同的编码方式读取配置文件
        encodings = ['utf-8', 'utf-8-sig', 'gbk', 'gb2312']
        success = False
        
        for encoding in encodings:
            try:
                config.read(config_file, encoding=encoding)
                if config.has_section('tushare'):
                    success = True
                    break
            except:
                continue
        
        if not success:
            raise ValueError(f"无法读取配置文件 {config_file}，请检查文件格式是否正确！")
        
        # 读取token
        if not config.has_option('tushare', 'token'):
            raise ValueError("配置文件中缺少 token 配置项！")
        
        token = config.get('tushare', 'token')
        if token == 'YOUR_TUSHARE_TOKEN_HERE' or not token:
            raise ValueError("请先在 config.ini 文件中配置你的 Tushare API Token！")
        
        # 设置 token
        ts.set_token(token)
        self.pro = ts.pro_api()
        
        # 读取筛选条件配置
        if not config.has_option('filter', 'min_circ_mv'):
            self.min_circ_mv = 50.0  # 默认值
        else:
            self.min_circ_mv = config.getfloat('filter', 'min_circ_mv')
        
        print("初始化完成，Tushare API 已连接")
        print(f"筛选条件：流通市值 >= {self.min_circ_mv} 亿元")
    
    def get_trade_dates(self, days=10):
        """获取最近的交易日期"""
        end_date = datetime.now().strftime('%Y%m%d')
        start_date = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
        
        df = self.pro.trade_cal(exchange='SSE', start_date=start_date, end_date=end_date, is_open='1')
        dates = df['cal_date'].tolist()
        dates.sort(reverse=True)
        return dates[:days]
    
    def get_stock_list(self):
        """获取所有股票列表，过滤掉ST股票"""
        print("\n[1/3] 正在获取股票列表...")
        
        # 获取所有A股
        stock_list = self.pro.stock_basic(exchange='', list_status='L', 
                                         fields='ts_code,symbol,name,area,industry,list_date')
        
        # 过滤ST股票
        stock_list = stock_list[~stock_list['name'].str.contains('ST', na=False)]
        
        print(f"      ✓ 获取到 {len(stock_list)} 只非ST股票")
        return stock_list

    def get_circ_mv_map(self, trade_date):
        """批量获取指定交易日流通市值，并按最小市值过滤"""
        print(f"\n[2/3] 正在批量获取 {trade_date} 的流通市值...")
        try:
            df = self.pro.daily_basic(
                trade_date=trade_date,
                fields='ts_code,circ_mv'
            )
            if df.empty:
                print("      × 未获取到流通市值数据")
                return {}

            df['circ_mv'] = pd.to_numeric(df['circ_mv'], errors='coerce')
            df = df.dropna(subset=['circ_mv'])
            df = df[df['circ_mv'] >= self.min_circ_mv]

            circ_mv_map = dict(zip(df['ts_code'], df['circ_mv']))
            print(f"      ✓ 满足流通市值条件: {len(circ_mv_map)} 只")
            return circ_mv_map
        except Exception as e:
            print(f"批量获取流通市值失败: {e}")
            return {}

    def get_market_daily_map(self, trade_dates, candidate_codes=None):
        """按交易日批量获取行情，并转为 ts_code -> 历史DataFrame 映射"""
        print(f"\n[3/3] 正在按交易日批量获取近 {len(trade_dates)} 天行情...")
        all_daily = []
        candidate_set = set(candidate_codes) if candidate_codes is not None else None

        for trade_date in trade_dates:
            try:
                df = self.pro.daily(
                    trade_date=trade_date,
                    fields='ts_code,trade_date,close,pct_chg,vol'
                )
                if candidate_set is not None and not df.empty:
                    df = df[df['ts_code'].isin(candidate_set)]
                if not df.empty:
                    all_daily.append(df)
            except Exception as e:
                print(f"获取 {trade_date} 行情失败: {e}")

        if not all_daily:
            print("      × 未获取到行情数据")
            return {}

        merged_df = pd.concat(all_daily, ignore_index=True)
        merged_df = merged_df.sort_values(['ts_code', 'trade_date'])

        grouped = {}
        for ts_code, group in merged_df.groupby('ts_code', sort=False):
            grouped[ts_code] = group.reset_index(drop=True)

        print(f"      ✓ 已获取 {len(grouped)} 只股票的近 {len(trade_dates)} 天行情")
        return grouped
    
    def get_stock_basic_info(self, ts_code, trade_date):
        """获取股票基本信息（流通市值）"""
        try:
            # 获取日线行情（包含流通市值）
            df = self.pro.daily_basic(ts_code=ts_code, trade_date=trade_date,
                                     fields='ts_code,trade_date,circ_mv')
            if df.empty:
                return None
            return df.iloc[0]['circ_mv']  # 返回流通市值（亿元）
        except Exception as e:
            print(f"获取 {ts_code} 基本信息失败: {e}")
            return None
    
    def get_stock_data(self, ts_code, start_date, end_date):
        """获取股票历史数据"""
        try:
            df = self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df.empty:
                return None
            
            # 按日期升序排列
            df = df.sort_values('trade_date')
            return df
        except Exception as e:
            print(f"获取 {ts_code} 数据失败: {e}")
            return None
    
    def check_volume_surge(self, df, day_idx):
        """检查某天是否放量超过100%"""
        if day_idx >= len(df) or day_idx + 1 >= len(df):
            return False
        
        current_vol = df.iloc[day_idx]['vol']
        prev_vol = df.iloc[day_idx + 1]['vol']
        
        if prev_vol == 0:
            return False
        
        # 放量超过100%，即当前成交量是前一天的2倍以上
        return current_vol / prev_vol > 2.0
    
    def check_price_increase(self, df, day_idx, threshold=3.8):
        """检查某天涨幅是否大于等于阈值"""
        if day_idx >= len(df):
            return False
        
        pct_chg = df.iloc[day_idx]['pct_chg']
        return pct_chg >= threshold
    
    def check_volume_shrink(self, df, day_idx):
        """检查某天是否缩量超过30%"""
        if day_idx >= len(df) or day_idx + 1 >= len(df):
            return False
        
        current_vol = df.iloc[day_idx]['vol']
        prev_vol = df.iloc[day_idx + 1]['vol']
        
        if prev_vol == 0:
            return False
        
        # 缩量超过30%，即当前成交量不到前一天的70%
        return current_vol / prev_vol < 0.7
    
    def check_continuous_shrink_decline(self, df, start_idx, end_idx):
        """检查连续缩量下跌"""
        if end_idx >= len(df):
            return False
        
        for i in range(start_idx, end_idx):
            if i >= len(df) or i + 1 >= len(df):
                return False
            
            current_vol = df.iloc[i]['vol']
            prev_vol = df.iloc[i + 1]['vol']
            current_price = df.iloc[i]['close']
            prev_price = df.iloc[i + 1]['close']
            
            # 检查是否缩量
            if prev_vol == 0 or current_vol >= prev_vol:
                return False
            
            # 检查是否下跌
            if current_price >= prev_price:
                return False
        
        return True
    
    def check_pattern(self, df, pattern_days):
        """
        检查特定模式
        pattern_days: 3, 4, 或 5
        
        逻辑说明（以3日模式为例）：
        假设今天是第0天（最新）
        - 第1个交易日（第3天前，索引2）：放量超过100%，且涨幅>=3.8%
        - 第2个交易日（第2天前，索引1）：缩量超过30%
        - 第2-3个交易日（第2天前到今天）：连续缩量下跌
        
        数据排列（从旧到新）：
        索引 2 = 第3天前 = 第1个交易日（放量上涨）
        索引 1 = 第2天前 = 第2个交易日（缩量）
        索引 0 = 今天 = 第3个交易日（继续缩量下跌）
        """
        if len(df) < pattern_days:
            return False
        
        # 反转数据，使索引0为最新交易日，索引越大越久远
        df = df.iloc[::-1].reset_index(drop=True)
        
        # 第1个交易日 = 第pattern_days天前
        first_day_idx = pattern_days - 1
        
        # 检查第1个交易日（N天前）：放量超过100%，且涨幅>=3.8%
        if first_day_idx >= len(df):
            return False
            
        # 检查放量：与它的前一天比较（即N+1天前）
        if first_day_idx + 1 >= len(df):
            return False
        
        current_vol = df.iloc[first_day_idx]['vol']
        prev_vol = df.iloc[first_day_idx + 1]['vol']
        
        if prev_vol == 0 or current_vol / prev_vol <= 2.0:
            return False
        
        # 检查涨幅
        if df.iloc[first_day_idx]['pct_chg'] < 3.8:
            return False
        
        # 第2个交易日及之后：连续缩量下跌
        # 从第2个交易日（first_day_idx - 1）到今天（索引0）
        for i in range(first_day_idx - 1, -1, -1):
            current_vol = df.iloc[i]['vol']
            prev_vol = df.iloc[i + 1]['vol']  # 前一天的成交量
            current_close = df.iloc[i]['close']
            prev_close = df.iloc[i + 1]['close']
            
            # 检查是否缩量（成交量比前一天少）
            if prev_vol == 0 or current_vol >= prev_vol:
                return False
            
            # 检查是否下跌（收盘价比前一天低）
            if current_close >= prev_close:
                return False
        
        # 额外检查第2个交易日是否缩量超过30%
        second_day_idx = first_day_idx - 1
        if second_day_idx >= 0:
            second_vol = df.iloc[second_day_idx]['vol']
            first_vol = df.iloc[first_day_idx]['vol']
            if first_vol == 0 or second_vol / first_vol >= 0.7:
                return False
        
        return True
    
    def screen_stocks_parallel(self):
        """
        并行筛选符合条件的股票
        对每只股票同时检查3种模式（3日、4日、5日）
        """
        print(f"\n{'='*60}")
        print(f"开始并行筛选股票（同时检查3种模式）")
        print(f"{'='*60}\n")
        
        # 获取股票列表
        stock_list = self.get_stock_list()
        
        # 获取交易日期（5日模式至少需要6个交易日，额外多取用于容错）
        trade_dates = self.get_trade_dates(days=10)
        if len(trade_dates) < 6:
            print("交易日期不足，无法筛选")
            return pd.DataFrame(), {'3日': 0, '4日': 0, '5日': 0}
        
        latest_date = trade_dates[0]
        lookback_dates = trade_dates[:8] if len(trade_dates) >= 8 else trade_dates

        # 批量获取流通市值，一次性过滤候选池
        circ_mv_map = self.get_circ_mv_map(latest_date)
        if not circ_mv_map:
            print("未获取到可用的流通市值数据")
            return pd.DataFrame(), {'3日': 0, '4日': 0, '5日': 0}

        # 仅保留满足市值条件的股票代码
        candidate_codes = stock_list[stock_list['ts_code'].isin(circ_mv_map.keys())]['ts_code'].tolist()
        if not candidate_codes:
            print("没有股票满足流通市值条件")
            return pd.DataFrame(), {'3日': 0, '4日': 0, '5日': 0}
        print(f"      ✓ 候选股票数（市值过滤后）: {len(candidate_codes)} 只")

        # 按交易日批量获取行情，避免逐股请求
        market_daily_map = self.get_market_daily_map(lookback_dates, candidate_codes)
        if not market_daily_map:
            print("未获取到可用的行情数据")
            return pd.DataFrame(), {'3日': 0, '4日': 0, '5日': 0}
        
        results = []
        pattern_stats = {'3日': 0, '4日': 0, '5日': 0}
        total = len(candidate_codes)
        stock_info_map = stock_list.set_index('ts_code')
        
        # 创建进度条
        pbar = tqdm(total=total, 
                   desc="筛选进度", 
                   unit="股",
                   ncols=100,
                   bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')
        
        for ts_code in candidate_codes:
            # 获取该股票的历史数据（已提前批量拉取）
            df = market_daily_map.get(ts_code)
            if df is None or len(df) < 3:
                pbar.update(1)
                continue
            
            # 同时检查3种模式
            match_patterns = []
            if len(df) >= 5 and self.check_pattern(df, 5):
                match_patterns.append('5日')
                pattern_stats['5日'] += 1
            
            if len(df) >= 4 and self.check_pattern(df, 4):
                match_patterns.append('4日')
                pattern_stats['4日'] += 1
            
            if len(df) >= 3 and self.check_pattern(df, 3):
                match_patterns.append('3日')
                pattern_stats['3日'] += 1
            
            # 如果符合任意一种模式，记录该股票
            if match_patterns:
                stock_row = stock_info_map.loc[ts_code]
                if isinstance(stock_row, pd.DataFrame):
                    stock_row = stock_row.iloc[0]

                # 获取最新数据
                latest_data = df.iloc[-1]
                circ_mv = circ_mv_map.get(ts_code, 0)
                results.append({
                    '股票代码': ts_code,
                    '股票名称': stock_row.get('name', ''),
                    '最新日期': latest_data['trade_date'],
                    '最新收盘价': latest_data['close'],
                    '最新涨跌幅(%)': latest_data['pct_chg'],
                    '流通市值(亿)': round(circ_mv, 2),
                    '所属行业': stock_row.get('industry', ''),
                    '符合模式': ', '.join(match_patterns),
                    '5日模式': '✓' if '5日' in match_patterns else '',
                    '4日模式': '✓' if '4日' in match_patterns else '',
                    '3日模式': '✓' if '3日' in match_patterns else ''
                })
                # 更新进度条描述，显示找到的股票数
                pbar.set_postfix({
                    '已找到': len(results),
                    '5日': pattern_stats['5日'],
                    '4日': pattern_stats['4日'],
                    '3日': pattern_stats['3日']
                })
            
            pbar.update(1)
        
        pbar.close()
        print(f"\n✓ 筛选完成！")
        print(f"  - 符合5日模式: {pattern_stats['5日']} 只")
        print(f"  - 符合4日模式: {pattern_stats['4日']} 只")
        print(f"  - 符合3日模式: {pattern_stats['3日']} 只")
        print(f"  - 符合任意模式的股票总数: {len(results)} 只\n")
        
        return pd.DataFrame(results), pattern_stats
    
    def run(self):
        """运行主程序"""
        print("\n" + "="*60)
        print("           股票筛选程序 (并行模式)")
        print("="*60)
        
        start_time = time.time()
        
        # 并行筛选3种模式
        print("\n[2/3] 开始筛选股票（每只股票同时检查3种模式）...")
        all_results, pattern_stats = self.screen_stocks_parallel()
        
        # 按模式分类结果
        result_5days = all_results[all_results['5日模式'] == '✓'].copy() if not all_results.empty else pd.DataFrame()
        result_4days = all_results[all_results['4日模式'] == '✓'].copy() if not all_results.empty else pd.DataFrame()
        result_3days = all_results[all_results['3日模式'] == '✓'].copy() if not all_results.empty else pd.DataFrame()
        
        # 导出到Excel
        print("[3/3] 正在导出结果到Excel...")
        output_file = f"选股结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            # 汇总表：包含所有符合条件的股票及其符合的模式
            if not all_results.empty:
                all_results.to_excel(writer, sheet_name='汇总（所有）', index=False)
            
            # 按模式分类的表
            if not result_5days.empty:
                result_5days.to_excel(writer, sheet_name='5日模式', index=False)
            
            if not result_4days.empty:
                result_4days.to_excel(writer, sheet_name='4日模式', index=False)
            
            if not result_3days.empty:
                result_3days.to_excel(writer, sheet_name='3日模式', index=False)
        
        # 计算总耗时
        elapsed_time = time.time() - start_time
        minutes = int(elapsed_time // 60)
        seconds = int(elapsed_time % 60)
        
        print(f"      ✓ 文件已保存\n")
        print("="*60)
        print("                   筛选结果汇总")
        print("="*60)
        print(f"输出文件: {output_file}")
        print(f"-" * 60)
        print(f"符合任意模式的股票: {len(all_results):>4} 只")
        print(f"-" * 60)
        print(f"  ├─ 5日模式: {pattern_stats.get('5日', 0):>4} 只")
        print(f"  ├─ 4日模式: {pattern_stats.get('4日', 0):>4} 只")
        print(f"  └─ 3日模式: {pattern_stats.get('3日', 0):>4} 只")
        print(f"-" * 60)
        print(f"总耗时: {minutes} 分 {seconds} 秒")
        print(f"平均速度: 约 {len(all_results)/max(elapsed_time/60, 0.01):.1f} 只/分钟")
        print("="*60)
        print("\n说明：")
        print("  • '汇总（所有）'工作表包含所有符合条件的股票")
        print("  • 其他工作表按模式分类显示")
        print("  • 一只股票可能同时符合多种模式")
        print("="*60)


if __name__ == "__main__":
    try:
        selector = StockSelector()
        selector.run()
    except Exception as e:
        print(f"程序运行出错: {e}")
        import traceback
        traceback.print_exc()

