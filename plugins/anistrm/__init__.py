
#**主要修改点总结：**

#1. **头部引用**：添加了 `from urllib.parse import quote`。
#2. **`__get_season_list`**：新增方法，动态获取“当前季度”和“上一季度”的时间字符串（如 `2024-10` 和 `2024-7`），以覆盖半年番或跨季补全。
#3. **`get_current_season_list`**：重构了获取逻辑，不再依赖单一的 `self._date` 变量，而是在获取列表时直接生成好带有正确季度路径的下载直链。
#4. **`__task`**：统一了增量和全量更新的逻辑，都通过传递具体的 `link` 给生成函数。

import os
import time
from datetime import datetime, timedelta
from urllib.parse import quote  # 新增引用

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.utils.http import RequestUtils
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
import xml.dom.minidom
from app.utils.dom import DomUtils


def retry(ExceptionToCheck: Any,
          tries: int = 3, delay: int = 3, backoff: int = 1, logger: Any = None, ret: Any = None):
    """
    :param ExceptionToCheck: 需要捕获的异常
    :param tries: 重试次数
    :param delay: 延迟时间
    :param backoff: 延迟倍数
    :param logger: 日志对象
    :param ret: 默认返回
    """

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay
            while mtries > 0:
                try:
                    return f(*args, **kwargs)
                except ExceptionToCheck as e:
                    msg = f"未获取到文件信息，{mdelay}秒后重试 ..."
                    if logger:
                        logger.warn(msg)
                    else:
                        print(msg)
                    time.sleep(mdelay)
                    mtries -= 1
                    mdelay *= backoff
            if logger:
                logger.warn('请确保当前季度番剧文件夹存在或检查网络问题')
            return ret

        return f_retry

    return deco_retry


class ANiStrm(_PluginBase):
    # 插件名称
    plugin_name = "ANiStrm"
    # 插件描述
    plugin_desc = "自动获取当季所有番剧，免去下载，轻松拥有一个番剧媒体库"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/honue/MoviePilot-Plugins/main/icons/anistrm.png"
    # 插件版本
    plugin_version = "2.4.3" # 版本号微调以示区别
    # 插件作者
    plugin_author = "honue"
    # 作者主页
    author_url = "https://github.com/honue"
    # 插件配置项ID前缀
    plugin_config_prefix = "anistrm_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _onlyonce = False
    _fulladd = False
    _storageplace = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._fulladd = config.get("fulladd")
            self._storageplace = config.get("storageplace")
            # 加载模块
        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._enabled and self._cron:
                try:
                    self._scheduler.add_job(func=self.__task,
                                            trigger=CronTrigger.from_crontab(self._cron),
                                            name="ANiStrm文件创建")
                    logger.info(f'ANi-Strm定时任务创建成功：{self._cron}')
                except Exception as err:
                    logger.error(f"定时任务配置错误：{str(err)}")

            if self._onlyonce:
                logger.info(f"ANi-Strm服务启动，立即运行一次")
                self._scheduler.add_job(func=self.__task, args=[self._fulladd], trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="ANiStrm文件创建")
                # 关闭一次性开关 全量转移
                self._onlyonce = False
                self._fulladd = False
            self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __get_season_list(self) -> List[str]:
        """
        获取当前季度和上一季度的列表 (格式: YYYY-M)
        用于覆盖半年番或跨季度的番剧
        """
        current_date = datetime.now()
        year = current_date.year
        month = current_date.month

        # 季度起始月份
        q_starts = [1, 4, 7, 10]
        
        # 找到小于等于当前月份的最大季度起始月
        curr_q = max([m for m in q_starts if m <= month])
        curr_idx = q_starts.index(curr_q)
        
        seasons = []
        
        # 1. 添加当前季度
        seasons.append(f"{year}-{curr_q}")
        
        # 2. 添加上一季度 (处理跨年情况)
        if curr_idx == 0:
            prev_q = 10
            prev_year = year - 1
        else:
            prev_q = q_starts[curr_idx - 1]
            prev_year = year
        seasons.append(f"{prev_year}-{prev_q}")
        
        return seasons

    @retry(Exception, tries=3, logger=logger, ret=[])
    def get_current_season_list(self) -> List[Dict]:
        """
        获取当前季(含上一季)所有番剧列表，直接生成下载链接
        返回格式统一为 [{'title': '...', 'link': '...'}]
        """
        all_files = []
        
        # 遍历当前季和上一季
        for season in self.__get_season_list():
            url = f'https://openani.an-i.workers.dev/{season}/'
            try:
                rep = RequestUtils(ua=settings.USER_AGENT if settings.USER_AGENT else None,
                                   proxies=settings.PROXY if settings.PROXY else None).post(url=url)
                
                # 检查响应状态
                if rep.status_code != 200:
                    continue

                files_json = rep.json().get('files', [])
                
                for file in files_json:
                    file_name = file['name']
                    # 直接根据所在季度生成完整链接，解决跨季度链接生成错误的问题
                    encoded_filename = quote(file_name, safe='')
                    # 构造直链
                    src_url = f'https://openani.an-i.workers.dev/{season}/{encoded_filename}.mp4?d=true'
                    
                    all_files.append({
                        'title': file_name,
                        'link': src_url
                    })
            except Exception as e:
                logger.warn(f"解析季度 {season} 数据时出错: {e}")
                continue
                
        return all_files

    @retry(Exception, tries=3, logger=logger, ret=[])
    def get_latest_list(self) -> List:
        addr = 'https://api.ani.rip/ani-download.xml'
        ret = RequestUtils(ua=settings.USER_AGENT if settings.USER_AGENT else None,
                           proxies=settings.PROXY if settings.PROXY else None).get_res(addr)
        ret_xml = ret.text
        ret_array = []
        # 解析XML
        dom_tree = xml.dom.minidom.parseString(ret_xml)
        rootNode = dom_tree.documentElement
        items = rootNode.getElementsByTagName("item")
        for item in items:
            rss_info = {}
            # 标题
            title = DomUtils.tag_value(item, "title", default="")
            # 链接
            link = DomUtils.tag_value(item, "link", default="")
            rss_info['title'] = title
            rss_info['link'] = link.replace("resources.ani.rip", "openani.an-i.workers.dev")
            ret_array.append(rss_info)
        return ret_array

    def __touch_strm_file(self, file_name, file_url: str) -> bool:
        """
        创建 strm 文件
        """
        if not file_url:
            return False

        # 检查API获取的URL格式是否符合要求
        if self._is_url_format_valid(file_url):
            # 格式符合要求，直接使用
            src_url = file_url
        else:
            # 格式不符合要求，进行转换
            src_url = self._convert_url_format(file_url)
        
        # 确保目录存在
        if not os.path.exists(self._storageplace):
            try:
                os.makedirs(self._storageplace)
            except Exception:
                pass

        file_path = f'{self._storageplace}/{file_name}.strm'
        if os.path.exists(file_path):
            # logger.debug(f'{file_name}.strm 文件已存在')
            return False
        try:
            with open(file_path, 'w', encoding='utf-8') as file:
                file.write(src_url)
                logger.debug(f'创建 {file_name}.strm 文件成功')
                return True
        except Exception as e:
            logger.error('创建strm源文件失败：' + str(e))
            return False

    def _is_url_format_valid(self, url: str) -> bool:
        """检查URL格式是否符合要求（.mp4?d=true）"""
        return url.endswith('.mp4?d=true')

    def _convert_url_format(self, url: str) -> str:
        """将URL转换为符合要求的格式"""
        if '?d=mp4' in url:
            # 将 ?d=mp4 替换为 .mp4?d=true
            return url.replace('?d=mp4', '.mp4?d=true')
        elif url.endswith('.mp4'):
            # 如果已经以.mp4结尾，添加?d=true
            return f'{url}?d=true'
        else:
            # 其他情况，添加.mp4?d=true
            return f'{url}.mp4?d=true'

    def __task(self, fulladd: bool = False):
        cnt = 0
        rss_info_list = []

        # 1. 获取文件列表
        if not fulladd:
            # 增量模式：使用 RSS 获取最新
            # logger.info("开始执行 ANiStrm 增量更新任务...")
            rss_info_list = self.get_latest_list()
        else:
            # 全量模式：扫描目录 (当前季+上一季)
            logger.info("开始执行 ANiStrm 全量扫描任务 (含当前季及上一季)...")
            rss_info_list = self.get_current_season_list()

        logger.info(f'本次处理 {len(rss_info_list)} 个文件信息')
        
        # 2. 统一处理文件创建
        for rss_info in rss_info_list:
            if self.__touch_strm_file(file_name=rss_info['title'], file_url=rss_info['link']):
                cnt += 1
                
        logger.info(f'新创建了 {cnt} 个strm文件')

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'fulladd',
                                            'label': '下次创建当前季度所有番剧strm',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 ? ? ?'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'storageplace',
                                            'label': 'Strm存储地址',
                                            'placeholder': '/downloads/strm'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '自动从open ANi抓取下载直链生成strm文件，免去人工订阅下载' + '\n' +
                                                    '配合目录监控使用，strm文件创建在/downloads/strm' + '\n' +
                                                    '通过目录监控转移到link媒体库文件夹 如/downloads/link/strm  mp会完成刮削',
                                            'style': 'white-space: pre-line;'
                                        }
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'emby容器需要设置代理，docker的环境变量必须要有http_proxy代理变量，大小写敏感，具体见readme.' + '\n' +
                                                    'https://github.com/honue/MoviePilot-Plugins',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "fulladd": False,
            "storageplace": '/downloads/strm',
            "cron": "*/20 22,23,0,1 * * *",
        }

    def __update_config(self):
        self.update_config({
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "enabled": self._enabled,
            "fulladd": self._fulladd,
            "storageplace": self._storageplace,
        })

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))


if __name__ == "__main__":
    anistrm = ANiStrm()
    name_list = anistrm.get_latest_list()
    print(name_list)

```
