import os
from os import path
from os import environ
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
import logging
from datetime import datetime, timedelta
import pytz
import time

dbconn = None
__sTunnel = None

def nowbyTZ(tzName):
    tformats = '%Y-%m-%d %H:%M:%S'
    tzinfo = time.tzname
    tNow = datetime.now()
    logging.info(f'Local System TimeZone info: {tzinfo[0]} and local time: {tNow}')

    targetNow = datetime.strptime(tNow.astimezone(pytz.timezone(tzName)).strftime(tformats), tformats)
    logging.info(f'Target TimeZone {tzName}:   Target time: {targetNow}')
    return targetNow

def get_Symbollist(listname):
    basedir = environ.get("PROD_LIST_DIR")
    logging.debug(f'Load symbol list {listname} from {basedir}')
    listpath = os.path.join(basedir, f'{listname}.csv')
    s_list = pd.read_csv(listpath)['Symbol'].unique()
    return s_list

def get_DBengine():
    global dbconn
    if dbconn is None:
        hostname=environ.get("DBHOST")
        uname=environ.get("DBUSER")
        pwd=environ.get("DBPWD")
        DB = environ.get("DBMKTDATA")
        DBPORT = environ.get("DBPORT")

        dbpath = "mysql+pymysql://{user}:{pw}@{host}:{port}/{db}".format(host=hostname, db=DB, user=uname, pw=pwd,port=DBPORT)
        logging.info(f'setup DBengine to {dbpath}')
        # Create SQLAlchemy engine to connect to MySQL Database
        dbconn = create_engine(dbpath)
        logging.info(f'dbconn=>{dbconn}')
    return dbconn

def get_Max_date(dbntable, symbol=None):
    try:
        if symbol is None:
            query = f"SELECT max(Date) as maxdate from {dbntable} ;"
        else:
            query = f"SELECT max(Date) as maxdate from {dbntable} where symbol = \'{symbol}\';"
        logging.info(f'get_Max_date :{query}')
        df = pd.read_sql(query, get_DBengine())
        max_date = df.maxdate.iloc[0]
        logging.info(f"get_Max_date() => {max_date} ")
        return max_date
    except Exception as e:
        logging.error("Exception occurred at get_Max_date()", exc_info=True)

def get_Max_Options_date(dbntable, symbol=None):
    try:
        if symbol is None:
            query = f'SELECT max(Date) as maxdate, section FROM {dbntable} group by section order by maxdate desc, section desc;'
        else:
            query = f'SELECT max(Date) as maxdate, section FROM {dbntable} where UnderlyingSymbol = \'{symbol}\' group by section order by maxdate desc, section desc;'

        logging.info(f'get_Max_date :{query}')
        df = pd.read_sql(query, get_DBengine())
        logging.debug(df.head())
        max_date = df.maxdate.iloc[0]
        section = df.section.iloc[0]
        logging.info(f"get_Max_date() => {max_date},{section} ")
        return max_date, section
    except Exception as e:
        logging.error("Exception occurred at get_Max_Options_date()", exc_info=True)

def get_Latest_row_by_Symbol(dbntable, symbol):
    try:
        query = f"SELECT * from {dbntable} where Symbol = \'{symbol}\' order by Date desc;"
        logging.info(f'get_Latest_row_by_Symbol :{query}')
        df = pd.read_sql(query, get_DBengine())
        if len(df) > 0:
            max_row = df.iloc[0]
        else:
            max_row = None
        return max_row
    except Exception as e:
        logging.error("Exception occurred at get_Latest_row_by_Symbol()", exc_info=True)

def ExecSQL(query):
    logging.info(f"ExecSQL: {query}")
    try:
        results = get_DBengine().execute(query)
        logging.info(f'number of rows execed: {results.rowcount}')
    except Exception as e:
        logging.error("Exception occurred at load_df(np.linspace)", exc_info=True)

def load_df_SQL(query):
    """
    Return dataframe from SQL statement
    """
    logging.info(f'load_df_SQL({query}).')
    try:
        df = pd.read_sql(query, get_DBengine())
        return df
    except Exception as e:
        logging.error("Exception occurred at load_df_SQL(np.linspace)", exc_info=True)

def load_df(stock_symbol=None, DailyMode=True, lastdt=None, startdt=None, dataMode="P"):
    """
    Return dataframe from histdailyprice3
    """
    HOST=environ.get("DBHOST")
    PORT=environ.get("DBPORT")
    USER=environ.get("DBUSER")
    PASSWORD=environ.get("DBPWD")
    logging.info(f'load_df ( {stock_symbol},{DailyMode},{lastdt},{startdt},{dataMode}).')    
    dpath = None
    if dataMode == "P":
        DBNAME=environ.get("DBMKTDATA")
        TBLName=environ.get("TBLDLYPRICE")
        if stock_symbol is not None:
            dpath = f"{TBLName}/{stock_symbol}.csv"
        elif (lastdt is not None) and (startdt is not None):
            dpath = f"{TBLName}/{startdt}-{lastdt}.csv"
    else:
        DBNAME=environ.get("DBPREDICT")
        TBLName=environ.get("TBLDAILYOUTPUT")

    if (dpath is not None) and path.isfile(dpath) and (not DailyMode):
        logging.info(f'Load data from {dpath}.')
        return pd.load_csv(dpath)
    else:
        logging.info(f'Load data from {DBNAME}.{TBLName} table in MySQL, Daily mode: {DailyMode}.')
        try: 
            wherecl=""
            if stock_symbol is not None:
                wherecl = f"where Symbol = '{stock_symbol}'"
            if startdt is not None:
                if len(wherecl) > 0:
                    wherecl = wherecl + f" and Date>='{startdt}'"
                else:
                    wherecl = f"where Date>='{startdt}'"
            if lastdt is not None:
                if len(wherecl) > 0:
                    wherecl = wherecl + f" and Date<='{lastdt}'"
                else:
                    wherecl = f"where Date<='{lastdt}'"
            nlimit = ""
            # if (stock_symbol is not None) and (startdt is None) and (lastdt is None):
            #     if DailyMode:
            #         nlimit = f" order by Date desc limit {DailySize}"
            #     else:
            #         nlimit = f" order by Date desc limit {TrainSize}"
            if dataMode == "P":
                query = f"SELECT Date, Symbol, Exchange, AdjClose, Close, Open, High, Low, Volume from {DBNAME}.{TBLName} {wherecl} {nlimit};"
            else:
                query = f"SELECT * from {DBNAME}.{TBLName} {wherecl} {nlimit};"

            logging.info(f'load_df query:{query}')
            histdailyprice3 = pd.read_sql(query, get_DBengine())
            # conn.close()
            df = histdailyprice3.copy()
            df = df.sort_values(by=['Date'])
            if (dpath is not None):
                df.to_csv(dpath, index=False)

            return df
        except Exception as e:
            logging.error("Exception occurred at load_df(np.linspace)", exc_info=True)

def StoreEOD(eoddata, DBn, TBLn):
    try:
        eoddata.head(3)
        logging.info(f'StoreEOD size: {len(eoddata)} in table:{TBLn} on DB:{DBn}')
        dbcon = get_DBengine()
        logging.info(f'StoreEOD dbcon: {dbcon}')
        # Convert dataframe to sql table
        eoddata.to_sql(name=TBLn, con=dbcon, schema=DBn, if_exists='append', index=False)
    except Exception as e:
        logging.error("Exception occurred", exc_info=True)

def StoreWebDaily(df):
    try:
        logging.info(f'StoreWebDaily size: {len(df)}')
        dbname=environ.get("DBWEB")
        table=environ.get("TBLWEBPREDICT")

        ExecSQL(f'TRUNCATE TABLE {dbname}.{table};')
        StoreEOD(df, dbname, table)
    except Exception as e:
        logging.error("Exception occurred at StoreDailyOutput()", exc_info=True)

def load_eod_price(ticker, start, end):
    DB = environ.get("DBMKTDATA")
    TBL = environ.get("TBLDLYPRICE")
    query = f"SELECT * from {DB}.{TBL} where symbol = \'{ticker}\' and Date >= \'{start}\' and Date <= \'{end}\' order by Date;"
    return load_df_SQL(query)

def load_symbols(symlistName):
    """
    # Return list of stock symbols.
    """

    PROD_LIST_DIR = environ.get("PROD_LIST_DIR")
    try:
        fpath = os.path.join(PROD_LIST_DIR, f'{symlistName}.csv')
        logging.info(f'Fetch symbols from {fpath}')
        stock_list = pd.read_csv(fpath)
        symbol_list = np.sort(stock_list.Symbol.unique())
        logging.debug(f'{symbol_list}')
        return symbol_list
    except Exception as e:
        logging.error("Exception occurred at load_symbols()", exc_info=True)

def load_symbols_dict():
    # Return list of stock symbols.

    PROD_LIST_DIR = environ.get("PROD_LIST_DIR")
    try:
        stock_list = pd.read_csv(os.path.join(PROD_LIST_DIR, "stock_exchange.csv"))
        return dict(stock_list.values)
    except Exception as e:
        logging.error("Exception occurred at load_symbols_dict()", exc_info=True)

def get_Last_Date_by_Sym(tblname, sym):
    FIRSTTRAINDTE = datetime.strptime(environ.get("FIRSTTRAINDTE"), "%Y/%m/%d").date()

    mktdate = get_Max_date(tblname, sym)
    lastdt = FIRSTTRAINDTE
    if mktdate is not None:
        lastdt = mktdate + timedelta(days=1)
    return lastdt