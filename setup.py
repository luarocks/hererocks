import codecs
import os
import setuptools

here = os.path.abspath(os.path.dirname(__file__))
readme = codecs.open(os.path.join(here, "README.rst"), encoding="UTF-8")
long_description = readme.read()
readme.close()

setuptools.setup(
    name="hererocks",
    version="0.25.1",
    description="Tool for installing Lua and LuaRocks locally",
    long_description=long_description,
    keywords="lua",
    url="https://github.com/luarocks/hererocks",
    # Original author
    author="Peter Melnichenko",
    # Contact email for current maintainers
    author_email="hisham@luarocks.org",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 2.7",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    py_modules=["hererocks"],
    entry_points={
        "console_scripts": [
            "hererocks=hererocks:main"
        ]
    }
)
