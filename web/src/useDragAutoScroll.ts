import { useEffect } from "react";

const EDGE = 130; // 距离窗口上 / 下沿多少像素以内开始自动滚动
const MAX_STEP = 34; // 贴着边时每一帧最多滚多少像素（越靠近边越快）

/**
 * 拖拽时靠近窗口上 / 下沿，页面自己滚动。
 *
 * 浏览器自带的拖拽自动滚动因环境而异（有的只认贴边的几个像素，有的干脆没有），而一天的内容有一千多像素高，
 * 从第 2 天拖到第 1 天经常拖不上去，所以这里自己做：监听 dragover 记下鼠标位置，用 requestAnimationFrame 连续滚动。
 * 同时兜底清理：拖拽结束（dragend / drop）时，去掉 body 上的 is-dragging（它会让按钮暂时穿透，不能一直留着）。
 */
export function useDragAutoScroll(enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    let y: number | null = null;
    let raf = 0;

    function tick() {
      raf = 0;
      if (y === null) return;
      const h = window.innerHeight;
      let dy = 0;
      if (y < EDGE) dy = -Math.ceil(((EDGE - y) / EDGE) * MAX_STEP);
      else if (y > h - EDGE) dy = Math.ceil(((y - (h - EDGE)) / EDGE) * MAX_STEP);
      if (dy !== 0) {
        window.scrollBy(0, dy);
        raf = requestAnimationFrame(tick); // 停在边上不动也会继续滚，直到离开边缘区
      }
    }

    const onOver = (e: DragEvent) => {
      y = e.clientY;
      if (!raf) raf = requestAnimationFrame(tick);
    };
    const stop = () => {
      y = null;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      document.body.classList.remove("is-dragging");
    };

    document.addEventListener("dragover", onOver);
    document.addEventListener("dragend", stop);
    document.addEventListener("drop", stop);
    return () => {
      document.removeEventListener("dragover", onOver);
      document.removeEventListener("dragend", stop);
      document.removeEventListener("drop", stop);
      stop();
    };
  }, [enabled]);
}
