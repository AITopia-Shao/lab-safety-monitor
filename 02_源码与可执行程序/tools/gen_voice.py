"""
生成 DFPlayer 语音文件（中文 TTS）。曲目编号与 config.yaml → events.voice 对应。
    pip install edge-tts
    python tools/gen_voice.py            # 输出到 voice_sd/mp3/0001.mp3 ...
然后把 voice_sd 里的 mp3 文件夹整个拷到 TF 卡根目录（FAT32 格式）。
没有网络时，也可以用任意 TTS 网站或自己录音，按同样的文件名保存。
"""
import os
import asyncio

VOICES = {
    1: "请佩戴护目镜",
    2: "请穿好实验服",
    3: "请佩戴防护手套",
    4: "请佩戴口罩",
    5: "请佩戴头罩",
    6: "实验区禁止饮食",
    7: "请勿在实验台伏案休息",
    8: "器皿靠近台面边缘，请移至内侧",
    9: "加热过程中请勿离开实验台",
    10: "检测到干烧，已自动切断电源",
    11: "检测到烟雾，已自动切断电源",
    12: "实验室湿度过高，请开启除湿",
}

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voice_sd", "mp3")


async def main():
    import edge_tts
    os.makedirs(OUT, exist_ok=True)
    for idx, text in VOICES.items():
        path = os.path.join(OUT, f"{idx:04d}.mp3")
        await edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural").save(path)
        print(path, text)


if __name__ == "__main__":
    asyncio.run(main())
