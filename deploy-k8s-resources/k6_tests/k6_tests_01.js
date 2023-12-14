import http from 'k6/http';
import { check } from 'k6';

const config_size = __ENV.ENTITY_CONFIG_SIZE || 1;

export const options = {
  vus: __ENV.K6_VUS || 50,
  duration: __ENV.k6_DURATION || '120s',
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
};
export default function () {
    let url;
    if (__ENV.ENTITY_CONFIG_SIZE > 1){
        let random = Math.floor(
            Math.sqrt(
              -2 *
                Math.floor((config_size * 0.1) / 1.3) *
                Math.floor((config_size * 0.1) / 1.3) *
                Math.log(Math.random())
            ) *
              Math.cos(2 * Math.PI * Math.random()) +
              Math.floor(config_size / 2)
          );
        
        url = `http://kong-kong-proxy.kong.svc.cluster.local/${random}route/json/valid`;    
    } else {
        url = `http://kong-kong-proxy.kong.svc.cluster.local/upstream/json/valid`;
    }

    const res = http.get(url, { timeout: '180s' });

    check(res, {
        'status is 200 for proxy request': (r) => r.status === 200,
    });
}

