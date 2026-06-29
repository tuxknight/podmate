"""PodMate setup.py"""

from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="podmate",
    version="0.1.0",
    description="Podcast 伴侣 — 下载、转写、翻译、配音",
    author="Nous Research",
    packages=find_packages(),
    include_package_data=True,
    install_requires=requirements,
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "podmate=podmate.cli:app",
        ],
    },
)
