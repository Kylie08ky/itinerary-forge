import "leaflet/dist/leaflet.css";
import "leaflet.markercluster/dist/MarkerCluster.css";
import "leaflet.markercluster/dist/MarkerCluster.Default.css";
import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { Layout } from "./offline";
import { LandingPage } from "./pages/LandingPage";
import { NewPlanPage } from "./pages/NewPlanPage";
import { PlanPage } from "./pages/PlanPage";
import { PlansPage } from "./pages/PlansPage";
import { SharePage } from "./pages/SharePage";
import { TodayPage } from "./pages/TodayPage";
import { AboutPage } from "./pages/AboutPage";
import { UsagePage } from "./pages/UsagePage";
import { DayTripsPage } from "./pages/DayTripsPage";
import { CulturePage } from "./pages/CulturePage";
import { TaskPage } from "./pages/TaskPage";
import "./styles.css";
import "./theme.css";
import "./cities.css";
import "./redesign.css";

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: "/", element: <LandingPage /> },
      { path: "/new", element: <NewPlanPage /> },
      { path: "/culture", element: <CulturePage /> },
      { path: "/daytrips", element: <DayTripsPage /> },
      { path: "/plans", element: <PlansPage /> },
      { path: "/plans/:planId", element: <PlanPage /> },
      { path: "/tasks/:taskId", element: <TaskPage /> },
      { path: "/s/:token", element: <SharePage /> },
      { path: "/today", element: <TodayPage /> },
      { path: "/usage", element: <UsagePage /> },
      { path: "/about", element: <AboutPage /> },
    ],
  },
]);

// Service Worker 只在生产构建里注册（开发服务器上它会缓存旧代码，调试时很烦）
if ("serviceWorker" in navigator && import.meta.env.PROD) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  });
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
);
