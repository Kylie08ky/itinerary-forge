import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Alternative, EditOp, Itinerary, PlanResponse, ValidationReport } from "./types";

/** 编辑中的一个快照：行程 + 它对应的质检结果。撤销 / 重做就是在快照之间来回。 */
export interface Snapshot {
  itinerary: Itinerary;
  report: ValidationReport;
}

/**
 * 行程页的「编辑中」状态（规格 M04）。
 *
 * - 编辑是无状态的：每次操作把当前行程和操作一起发给后端，后端算出新行程返回；
 * - 撤销栈只在这里（内存里），刷新页面就丢——所以有未保存修改时会提醒；
 * - 点「保存为新版本」才写进数据库。
 */
export function useEditSession(planId: string, onError: (message: string) => void) {
  const [current, setCurrent] = useState<Snapshot | null>(null);
  const [undoStack, setUndoStack] = useState<Snapshot[]>([]);
  const [redoStack, setRedoStack] = useState<Snapshot[]>([]);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState<string[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const currentRef = useRef<Snapshot | null>(null);
  currentRef.current = current;

  const active = current !== null;
  const dirty = undoStack.length > 0;

  const start = useCallback((base: Snapshot) => {
    setCurrent(base);
    setUndoStack([]);
    setRedoStack([]);
    setTouched([]);
    setNotes([]);
  }, []);

  const exit = useCallback(() => {
    setCurrent(null);
    setUndoStack([]);
    setRedoStack([]);
    setTouched([]);
    setNotes([]);
  }, []);

  const apply = useCallback(
    async (op: EditOp): Promise<boolean> => {
      const cur = currentRef.current;
      if (!cur) return false;
      setBusy(true);
      try {
        const res = await api.edit(planId, cur.itinerary, op);
        // 锁定 / 解锁没有改任何一天的内容，但同样要能撤销
        setUndoStack((s) => [...s, cur]);
        setRedoStack([]);
        setCurrent({ itinerary: res.itinerary, report: res.report });
        setTouched(res.touched);
        setNotes(res.notes);
        return true;
      } catch (err) {
        onError(err instanceof ApiError ? err.message : String(err));
        return false;
      } finally {
        setBusy(false);
      }
    },
    [planId, onError],
  );

  const undo = useCallback(() => {
    const cur = currentRef.current;
    setUndoStack((s) => {
      if (!cur || s.length === 0) return s;
      setRedoStack((r) => [...r, cur]);
      setCurrent(s[s.length - 1]);
      setTouched([]);
      setNotes([]);
      return s.slice(0, -1);
    });
  }, []);

  const redo = useCallback(() => {
    const cur = currentRef.current;
    setRedoStack((r) => {
      if (!cur || r.length === 0) return r;
      setUndoStack((s) => [...s, cur]);
      setCurrent(r[r.length - 1]);
      setTouched([]);
      setNotes([]);
      return r.slice(0, -1);
    });
  }, []);

  const alternatives = useCallback(
    async (placeId: string, indoorOnly = false): Promise<Alternative[]> => {
      const cur = currentRef.current;
      if (!cur) return [];
      try {
        return await api.alternatives(planId, cur.itinerary, placeId, indoorOnly);
      } catch (err) {
        onError(err instanceof ApiError ? err.message : String(err));
        return [];
      }
    },
    [planId, onError],
  );

  const save = useCallback(
    async (name: string, note: string): Promise<PlanResponse | null> => {
      const cur = currentRef.current;
      if (!cur) return null;
      setBusy(true);
      try {
        return await api.saveVersion(planId, cur.itinerary, name, note);
      } catch (err) {
        onError(err instanceof ApiError ? err.message : String(err));
        return null;
      } finally {
        setBusy(false);
      }
    },
    [planId, onError],
  );

  // 有未保存的修改时，关页面 / 刷新前让浏览器提醒一下
  useEffect(() => {
    if (!dirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);

  // 快捷键：Ctrl/Cmd+Z 撤销，Ctrl/Cmd+Shift+Z 或 Ctrl+Y 重做（在输入框里不拦截）
  useEffect(() => {
    if (!active) return;
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (!(e.ctrlKey || e.metaKey)) return;
      const k = e.key.toLowerCase();
      if (k === "z" && !e.shiftKey) {
        e.preventDefault();
        undo();
      } else if ((k === "z" && e.shiftKey) || k === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [active, undo, redo]);

  return {
    current,
    active,
    dirty,
    busy,
    touched,
    notes,
    canUndo: undoStack.length > 0,
    canRedo: redoStack.length > 0,
    editCount: undoStack.length,
    start,
    exit,
    apply,
    undo,
    redo,
    alternatives,
    save,
  };
}
