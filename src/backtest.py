from pathlib import Path
import pandas as pd
from pit import *

path_file = Path("../data/processed/13f_panel_sample.parquet")
df = pd.read_parquet(path_file)

snap = breadth(df, '2013-03-31', '2013-08-01')


print(snap.head(10))