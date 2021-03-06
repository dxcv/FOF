﻿# -*- coding: utf-8 -*-
"""
Created on Fri Feb 17 10:56:11 2017

@author: Administrator
"""

import pandas as pd
from fh_tools.windy_utils_rest import WindRest
from sqlalchemy.types import String, Date
from datetime import datetime, date, timedelta
import logging
from config_fh import get_db_engine, get_db_session, WIND_REST_URL, STR_FORMAT_DATE
logger = logging.getLogger()


def fund_nav_df_2_sql(table_name, fund_nav_df, engine, is_append=True):
    #    print('reorg dfnav data[%d, %d]' % fund_nav_df.shape)
    try:
        fund_nav_df['NAV_DATE'] = pd.to_datetime(fund_nav_df['NAV_DATE']).apply(lambda x: x.date())
    except Exception as exp:
        logger.exception(str(fund_nav_df['NAV_DATE']))
        return None
    trade_date_s = pd.to_datetime(fund_nav_df.index)
    trade_date_latest = trade_date_s.max().date()
    fund_nav_df['trade_date'] = trade_date_s
    fund_nav_df.rename(columns={
        'NAV_DATE': 'nav_date',
        'NAV': 'nav',
        'NAV_ACC': 'nav_acc',
    }, inplace=True)
    fund_nav_df.set_index(['wind_code', 'trade_date'], inplace=True)
    action_str = 'append' if is_append else 'replace'
    #    print('df--> sql fundnav table if_exists="%s"' % action_str)
    fund_nav_df.to_sql(table_name, engine, if_exists=action_str, index_label=['wind_code', 'trade_date'],
                       dtype={
                           'wind_code': String(200),
                           'nav_date': Date,
                           'trade_date': Date,
                       })  # , index=False
    logger.info('%d data inserted', fund_nav_df.shape[0])
    return trade_date_latest


def update_trade_date_latest(wind_code_trade_date_latest):
    params = [{'wind_code': wind_code, 'trade_date_latest': trade_date_latest}
              for wind_code, trade_date_latest in wind_code_trade_date_latest.items()]
    with get_db_session() as session:
        session.execute(
            'update fund_info set trade_date_latest = :trade_date_latest where wind_code = :wind_code',
            params)
    logger.info('%d funds update latest trade date', len(wind_code_trade_date_latest))


def import_wind_fund_nav_to_fund_nav():
    """
    将 wind_fund_nav 数据导入到 fund_nav 表中
    :return: 
    """
    sql_str = """insert fund_nav(wind_code, nav_date, nav, nav_acc, source_mark)
select wfn.wind_code, wfn.nav_date, wfn.nav, wfn.nav_acc, 0 source_mark 
from
(
select wind_code, nav_date, nav, nav_acc, 0
from wind_fund_nav
group by wind_code, nav_date
) as wfn
left outer join
fund_nav fn
on 
wfn.wind_code = fn.wind_code and 
wfn.nav_date = fn.nav_date
where fn.nav is null"""
    with get_db_session() as session:
        session.execute(sql_str)
    logger.info('wind_fund_nav has been imported to fund_nav table')


def update_wind_fund_nav(get_df=False, wind_code_list=None):
    table_name = 'wind_fund_nav'
    rest = WindRest(WIND_REST_URL)  # 初始化数据下载端口
    # 初始化数据库engine
    engine = get_db_engine()
    # 链接数据库，并获取fundnav旧表
    with get_db_session(engine) as session:
        table = session.execute('select wind_code, max(trade_date) from wind_fund_nav group by wind_code')
        fund_trade_date_latest_in_nav_dic = dict(table.fetchall())
        wind_code_set_existed_in_nav = set(fund_trade_date_latest_in_nav_dic.keys())
    # 获取wind_fund_info表信息
    fund_info_df = pd.read_sql_query(
        """SELECT DISTINCT wind_code as wind_code,fund_setupdate,fund_maturitydate,trade_date_latest
from fund_info group by wind_code""", engine)
    trade_date_latest_dic = {wind_code: trade_date_latest for wind_code, trade_date_latest in
                             zip(fund_info_df['wind_code'], fund_info_df['fund_setupdate'])}
    fund_info_df.set_index('wind_code', inplace=True)
    if wind_code_list is None:
        wind_code_list = list(fund_info_df.index)
    else:
        wind_code_list = list(set(wind_code_list) & set(fund_info_df.index))
    date_end = date.today() - timedelta(days=1)
    date_end_str = date_end.strftime(STR_FORMAT_DATE)
    fund_nav_all_df = []
    no_data_count = 0
    code_count = len(wind_code_list)
    # 对每个新获取的基金名称进行判断，若存在 fundnav 中，则只获取部分净值
    wind_code_trade_date_latest = {}
    try:
        for i, wind_code in enumerate(wind_code_list):
            # 设定数据获取的起始日期
            wind_code_trade_date_latest[wind_code] = date_end
            if wind_code in wind_code_set_existed_in_nav:
                trade_latest = fund_trade_date_latest_in_nav_dic[wind_code]
                if trade_latest >= date_end:
                    continue
                date_begin = trade_latest + timedelta(days=1)
            else:
                date_begin = trade_date_latest_dic[wind_code]
            if date_begin is None:
                continue
            elif isinstance(date_begin, str):
                date_begin = datetime.strptime(date_begin, STR_FORMAT_DATE).date()

            if isinstance(date_begin, date):
                if date_begin.year < 1900:
                    continue
                if date_begin > date_end:
                    continue
                date_begin = date_begin.strftime('%Y-%m-%d')
            else:
                continue

            # 尝试获取 fund_nav 数据
            for k in range(2):
                try:
                    fund_nav_tmp_df = rest.wsd(codes=wind_code, fields='nav,NAV_acc,NAV_date', begin_time=date_begin,
                                               end_time=date_end_str, options='Fill=Previous')
                    break
                except Exception as exp:
                    logger.error("%s Failed, ErrorMsg: %s" % (wind_code, str(exp)))
                    continue
            else:
                del wind_code_trade_date_latest[wind_code]
                fund_nav_tmp_df = None

            if fund_nav_tmp_df is None:
                logger.info('%s No data', wind_code)
                del wind_code_trade_date_latest[wind_code]
                no_data_count += 1
                logger.warning('%d funds no data', no_data_count)
            else:
                fund_nav_tmp_df.dropna(how='all', inplace=True)
                df_len = fund_nav_tmp_df.shape[0]
                if df_len == 0:
                    continue
                fund_nav_tmp_df['wind_code'] = wind_code
                # 此处删除 trade_date_latest 之后再加上，主要是为了避免因抛出异常而导致的该条数据也被记录更新
                del wind_code_trade_date_latest[wind_code]
                trade_date_latest = fund_nav_df_2_sql(table_name, fund_nav_tmp_df, engine, is_append=True)
                if trade_date_latest is None:
                    logger.error('%s[%d] data insert failed', wind_code)
                else:
                    wind_code_trade_date_latest[wind_code] = trade_date_latest
                    logger.info('%d) %s updated, %d funds left', i, wind_code, code_count - i)
                    if get_df:
                        fund_nav_all_df = fund_nav_all_df.append(fund_nav_tmp_df)
    finally:
        import_wind_fund_nav_to_fund_nav()
        update_trade_date_latest(wind_code_trade_date_latest)
        try:
            update_fund_mgrcomp_info()
        except:
            # 新功能上线前由于数据库表不存在，可能导致更新失败，属于正常现象
            logger.exception('新功能上线前由于数据库表不存在，可能导致更新失败，属于正常现象')
    return fund_nav_all_df


def update_fund_mgrcomp_info():
    """
    fund_info 表 及 fund_nav表更新完成后，更新及插入 fund_mgrcomp_info 表相关统计信息
    :return: 
    """
    with get_db_session() as session:
        # 对已存在数据更新 基金统计数据
        sql_str = """update
fund_mgrcomp_info fmi, (
	select fi.fund_mgrcomp, 
	fi.fund_maturitydate, 
	count(*) fund_count_tot, 
	sum(if(fi.fund_maturitydate is null or fi.fund_maturitydate <= '1900-01-01' or fi.fund_maturitydate > curdate(),1,0)) fund_count_existing,
	sum(if(datediff(curdate(), ifnull(fn.nav_date_max,'1900-01-01'))<30, 1, 0)) fund_count_active
from fund_info fi left outer join
	(select wind_code, max(nav_date) nav_date_max from fund_nav group by wind_code) fn
	on fi.wind_code = fn.wind_code
where fi.fund_mgrcomp is not null
	and fi.fund_mgrcomp not like '%%*%%'
group by fi.fund_mgrcomp
) fmi_new
set 
fmi.fund_count_tot = fmi_new.fund_count_tot,
fmi.fund_count_existing = fmi_new.fund_count_existing,
fmi.fund_count_active = fmi_new.fund_count_active
where fmi.name = fmi_new.fund_mgrcomp"""
        session.execute(sql_str)
        logger.info('update fund_mgrcomp_info finished')
        # 添加新基金管理人信息及统计数据
        sql_str = """insert into fund_mgrcomp_info(name, review_status, fund_count_tot,fund_count_existing,fund_count_active)
select fi.fund_mgrcomp, 
	0,
	count(*) fund_count_tot, 
	sum(if(fi.fund_maturitydate is null or fi.fund_maturitydate <= '1900-01-01' or fi.fund_maturitydate > curdate(),1,0)) fund_count_existing,
	sum(if(datediff(curdate(), ifnull(fn.nav_date_max,'1900-01-01'))<30, 1, 0)) fund_count_active
from fund_info fi left outer join
	(select wind_code, max(nav_date) nav_date_max from fund_nav group by wind_code) fn
	on fi.wind_code = fn.wind_code
where fi.fund_mgrcomp is not null
	and fi.fund_mgrcomp not like '%*%'
	group by fi.fund_mgrcomp
    having fi.fund_mgrcomp not in (select name from fund_mgrcomp_info)"""
    session.execute(sql_str)
    logger.info('insert new fund_mgrcomp into fund_mgrcomp_info finished')
    sql_str = """update 
fund_info fi, fund_mgrcomp_info fmi
set fi.mgrcomp_id = fmi.mgrcomp_id
where fi.fund_mgrcomp = fmi.name
and fi.mgrcomp_id is null"""
    session.execute(sql_str)
    logger.info('update mgrcomp_id on fund_mgrcomp_info finished')


def clean_fund_nav(date_str):
    """
    wind数据库中存在部分数据净值记录前后不一致的问题
    比如：某日记录净值 104，次一后期净值变为 1.04 导致净值收益率走势出现偏差
    此脚本主要目的在于对这种偏差进行修正
    :param date_str: 
    :return: 
    """
    sql_str = """select fn_before.wind_code, fn_before.nav_date nav_date_before, fn_after.nav_date nav_date_after, fn_before.nav_acc nav_acc_before, fn_after.nav_acc nav_acc_after, fn_after.nav_acc / fn_before.nav_acc nav_acc_pct
from
fund_nav fn_before,
fund_nav fn_after,
(
select wind_code, max(if(nav_date<%s, nav_date, null)) nav_date_before, min(if(nav_date>=%s, nav_date, null)) nav_date_after
from fund_nav group by wind_code
having nav_date_before is not null and nav_date_after is not null
) fn_date
where fn_before.nav_date = fn_date.nav_date_before and fn_before.wind_code = fn_date.wind_code
and fn_after.nav_date = fn_date.nav_date_after and fn_after.wind_code = fn_date.wind_code
and fn_after.nav_acc / fn_before.nav_acc < 0.5
    """
    engine = get_db_engine()
    data_df = pd.read_sql(sql_str, engine, params=[date_str, date_str])
    data_count = data_df.shape[0]
    if data_count == 0:
        logger.info('no data for clean on %s', date_str)
        return
    logger.info('\n%s', data_df)
    data_list = data_df.to_dict(orient='records')
    with get_db_session(engine) as session:
        for content in data_list:
            wind_code = content['wind_code']
            nav_date_before = content['nav_date_before']
            logger.info('update wind_code=%s nav_date<=%s', wind_code, nav_date_before)
            sql_str = "update fund_nav set nav = nav/100, nav_acc = nav_acc/100 where wind_code = :wind_code and nav_date <= :nav_date"
            session.execute(sql_str, params={'wind_code': wind_code, 'nav_date': nav_date_before})


if __name__ == '__main__':

    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s: %(levelname)s [%(name)s:%(funcName)s] %(message)s')
    # fund_info 表 及 fund_nav表更新完成后，更新及插入 fund_mgrcomp_info 表相关统计信息
    # update_fund_mgrcomp_info()

    # 调用wind接口更新基金净值
    # update_wind_fund_nav(get_df=False, wind_code_list=['XT1612161.XT'])

    # 将 wind_fund_nav 数据导入到 fund_nav 表中
    # import_wind_fund_nav_to_fund_nav()

    # wind数据库中存在部分数据净值记录前后不一致的问题
    # 比如：某日记录净值
    # 104，次一后期净值变为 1.04
    # 导致净值收益率走势出现偏差
    # 此脚本主要目的在于对这种偏差进行修正
    # date_list = pd.date_range('2017-05-1', '2017-06-15', freq='2D')
    # for datetimeobj in date_list:
    #     date_str = str(datetimeobj.date())
    #     clean_fund_nav(date_str)

