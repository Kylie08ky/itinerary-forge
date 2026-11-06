import type { PlaceImageInfo, PlaceIntroInfo } from "../types";
import { mediaSrc } from "../api";
import { useState } from "react";

/**
 * 地点照片 + 署名（规格 M02 S7b-1）：署名永远和图片一起显示；图片加载失败时整块（含署名）收起，不留破图。
 */
export function PlacePhoto({ image, name }: { image: PlaceImageInfo; name: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) return null;
  return (
    <figure className="place-photo" data-testid="place-photo">
      <img src={mediaSrc(image.url)} alt={name} crossOrigin="anonymous" loading="lazy" onError={() => setFailed(true)} />
      <figcaption data-testid="photo-credit">
        摄影：{image.author} ·{" "}
        {image.license_url ? (
          <a href={image.license_url} target="_blank" rel="noreferrer">
            {image.license}
          </a>
        ) : (
          image.license
        )}{" "}
        ·{" "}
        <a href={image.source_url} target="_blank" rel="noreferrer">
          来源 ↗
        </a>
      </figcaption>
    </figure>
  );
}

/** 简介 + 来源（规格 M02 S7b-1）：中文维基百科摘要，授权 CC BY-SA 4.0，来源链接永远显示。 */
export function PlaceIntro({ intro }: { intro: PlaceIntroInfo }) {
  return (
    <div className="place-intro" data-testid="place-intro">
      <p>{intro.text}</p>
      <div className="place-intro-src">
        来源：
        <a href={intro.source_url} target="_blank" rel="noreferrer">
          {intro.source_name} ↗
        </a>{" "}
        ·{" "}
        <a href={intro.license_url} target="_blank" rel="noreferrer">
          {intro.license}
        </a>
      </div>
    </div>
  );
}
