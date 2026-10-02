from setuptools import setup, find_packages
import pathlib

WORK_DIR = pathlib.Path(__file__).parent

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

__version__ = ""
exec(open('efb_wechat_comwechat_slave/__version__.py').read())


setup(
    name="efb-wechat-comwechat-slave",
    version=__version__,
    description='EFB Slave for WeChat on ComWeChat',
    author='honus',
    author_email="honusmr@gmail.com",
    url="https://github.com/0honus0/efb-wechat-comwechat-slave",
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    python_requires='>=3.7',
    keywords=["wechat", ],
    install_requires=[
        # 必须用 sddpljx 的 fork:支持 api_host/api_port 自定义 Hook 地址,
        # PyPI 原版 1.0.1 没有这两个参数,会导致 WeChatRobot 初始化失败
        "python-comwechatrobot-http @ git+https://github.com/ol1u/python-comwechatrobot-http.git",
        "ehforwarderbot",
        "PyYaml>=5.3",
        # python-telegram-bot~=13.15 (efb-telegram-master 依赖) 钉死 cachetools==4.2.2,
        # 不钉死的话 pip 会装 7.x,直接报 ContextualVersionConflict 起不来
        "cachetools==4.2.2",
        "requests",
        # 同上,PTB v13 需要 urllib3 v1(v2 删掉了 urllib3.contrib.appengine)
        "urllib3<2",
        "peewee",
        "python-magic",
        "lxml",
        "pilk",
        "pydub",
        "lottie",
        "cairosvg",
    ],
    long_description=long_description,
    long_description_content_type="text/markdown",
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: User Interfaces',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.7',
        "Operating System :: OS Independent"
    ],
    entry_points={
        'ehforwarderbot.slave': 'honus.comwechat = efb_wechat_comwechat_slave:ComWeChatChannel',
    }
)
