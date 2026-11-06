import { ActiveToday } from "../components/TodayCard";

/** 手机底栏「今天」进来的整页今日视图（规格 M15 S2d）。 */
export function TodayPage() {
  return (
    <div className="wrap">
      <ActiveToday big />
    </div>
  );
}
