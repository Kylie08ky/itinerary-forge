import { useEffect, useState } from "react";
import { api } from "../api";
import { dishSearches } from "../nav";
import type { FoodDish } from "../types";

const SHOWN = 4;

/**
 * 行程页的「当地名菜」（规格 10 H22）：这座城市的小吃 / 名菜，简介来自中文维基百科并链到来源。
 * 只展示，不进排点；「去哪吃」不存餐厅：国内点「在大众点评 / 小红书里搜」，国外点「在 Google 地图里搜」（用当地语言名）。
 * 没有名菜的城市整张卡不显示。
 */
export function DishesCard({ cityIds }: { cityIds: string[] }) {
  const key = cityIds.join(",");
  const [groups, setGroups] = useState<{ cityId: string; dishes: FoodDish[] }[]>([]);
  const [all, setAll] = useState<Record<string, boolean>>({});

  useEffect(() => {
    let live = true;
    Promise.all(
      cityIds.map((cityId) =>
        api
          .food(cityId)
          .then((dishes) => ({ cityId, dishes }))
          .catch(() => ({ cityId, dishes: [] as FoodDish[] })),
      ),
    ).then((rows) => {
      if (live) setGroups(rows.filter((g) => g.dishes.length > 0));
    });
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  if (groups.length === 0) return null;
  return (
    <section className="card dishes-card" data-testid="dishes-card">
      <h2>当地名菜</h2>
      {groups.map((g) => {
        const open = all[g.cityId];
        const shown = open ? g.dishes : g.dishes.slice(0, SHOWN);
        const first = g.dishes[0];
        return (
          <div key={g.cityId} className="dishes-city" data-testid={`dishes-${g.cityId}`}>
            {groups.length > 1 && <h3>{first.city_zh}</h3>}
            <ul className="dishes-list">
              {shown.map((d) => {
                const near = d.near ? { lat: d.near[0], lng: d.near[1] } : null;
                return (
                  <li key={d.id} data-testid={`dish-${d.id}`}>
                    <div className="dish-head">
                      <strong>{d.name_zh}</strong>
                      {d.name_local && <span className="muted"> · {d.name_local}</span>}
                    </div>
                    <p className="muted">{d.summary}</p>
                    <div className="dish-links">
                      {dishSearches(d, near).map((s) => (
                        <a key={s.key} href={s.href} target="_blank" rel="noreferrer" data-testid={`dish-search-${s.key}-${d.id}`}>
                          {s.label}
                        </a>
                      ))}
                      <a href={d.source.url} target="_blank" rel="noreferrer" className="muted">
                        来源 ↗
                      </a>
                    </div>
                  </li>
                );
              })}
            </ul>
            {g.dishes.length > SHOWN && (
              <button
                type="button"
                className="tool"
                data-testid={`dishes-more-${g.cityId}`}
                onClick={() => setAll((cur) => ({ ...cur, [g.cityId]: !open }))}
              >
                {open ? "收起" : `展开全部 ${g.dishes.length} 样`}
              </button>
            )}
            {first.generated && first.category_note && <p className="muted dishes-note">{first.category_note}</p>}
          </div>
        );
      })}
    </section>
  );
}
