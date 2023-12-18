import http from 'k6/http';
import { check } from 'k6';
import encoding from 'k6/encoding';

const config_size = __ENV.ENTITY_CONFIG_SIZE || 1;

export const options = {
  vus: __ENV.K6_VUS || 50,
  duration: __ENV.k6_DURATION || '120s',
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'],
};
export default function () {
    let url;
    let basic_auth_header;
    let key_auth_header;
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
        
        url = `https://kong-kong-proxy.kong.svc.cluster.local/${random}route/json/valid`; 
        basic_auth_header = 'Basic ' + encoding.b64encode(`testuser${random}:testuserpassword${random}`);
        key_auth_header = `testuserpassword${random}`;   
    } else {
        url = `https://kong-kong-proxy.kong.svc.cluster.local/upstream/json/valid`;
        basic_auth_header = 'Basic ' + encoding.b64encode(`testuser1:testuserpassword1`);
        key_auth_header = `testuserpassword1`;
    }

    let res;
    if (__ENV.BASIC_AUTH_ENABLED == 'true'){
        res = http.get(url, { timeout: '180s', headers: {'Authorization': basic_auth_header} });
    } else if (__ENV.KEY_AUTH_ENABLED == 'true') {
        res = http.get(url, { timeout: '180s', headers: {'apikey': key_auth_header} });
    } else {
        res = http.get(url, { timeout: '180s' });
    }

    check(res, {
        'status is 200 for proxy request': (r) => r.status === 200,
    });
}

