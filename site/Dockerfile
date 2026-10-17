# Build stage: generate dist/ from content.json (Python stdlib only).
FROM python:3.12-alpine AS build
WORKDIR /src
COPY . .
RUN python3 build.py --out /dist

# Serve stage: static files behind nginx on port 80.
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /dist /usr/share/nginx/html
EXPOSE 80
