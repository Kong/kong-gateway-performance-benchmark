import http from 'k6/http';
export const options = {
  vus: 10,
  duration: '300s',
};
export default function () {
  http.get('http://kong-kong-proxy.kong.svc.cluster.local/upstream/json/valid');
}