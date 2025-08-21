# quick_analysis.py
import pandas as pd, pathlib
p = pathlib.Path("./logs")
tr = pd.read_csv(p/"trades.csv")
wr = (tr["net"] > 0).mean()
exp = tr["r"].mean()        # expectancy in R
print(f"Trades: {len(tr)} | Win%: {wr*100:.1f}% | Avg R: {tr['r'].mean():.2f} | PF: {tr.loc[tr.net>0,'net'].sum()/abs(tr.loc[tr.net<0,'net'].sum()) :.2f}")
print(tr.groupby("symbol")["r"].agg(['count','mean']).sort_values('count',ascending=False).head(10))
