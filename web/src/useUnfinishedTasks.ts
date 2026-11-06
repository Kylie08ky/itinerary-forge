import { useEffect, useState } from "react";
import { api } from "./api";
import type { TaskCard } from "./types";

/**
 * 没生成完的任务（规格 07 P19）：「我的行程」的「正在生成」区和导航上的红点共用同一个轮询，
 * 有人订阅才轮询；有任务在跑时 2.5 秒一次，只剩失败的 / 没有任务时 10 秒一次；页面藏在后台时不发请求。
 */
type Listener = (cards: TaskCard[]) => void;

let current: TaskCard[] = [];
let timer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<Listener>();

function schedule() {
  if (timer) clearTimeout(timer);
  timer = undefined;
  if (listeners.size === 0) return;
  const busy = current.some((c) => c.status !== "failed");
  timer = setTimeout(() => void poll(), busy ? 2500 : 10_000);
}

async function poll() {
  if (!document.hidden) {
    try {
      current = await api.unfinishedTasks();
      listeners.forEach((l) => l(current));
    } catch {
      /* 连不上后端：保持上一次的结果，下次再试 */
    }
  }
  schedule();
}

/** 立刻刷新一次（比如刚提交了生成、刚移除了一张失败的卡）。 */
export function refreshUnfinishedTasks() {
  void poll();
}

export function useUnfinishedTasks(): TaskCard[] | null {
  const [cards, setCards] = useState<TaskCard[] | null>(null);
  useEffect(() => {
    listeners.add(setCards);
    void poll();
    return () => {
      listeners.delete(setCards);
      schedule();
    };
  }, []);
  return cards;
}
