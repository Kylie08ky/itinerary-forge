# 前端：构建静态资源，用 nginx 托管
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# 浏览器直连后端，地址在构建时注入（compose 里传 host 映射端口）
ARG VITE_API_BASE=http://localhost:8137
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
