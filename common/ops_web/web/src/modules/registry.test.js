import { beforeEach, describe, expect, it } from 'vitest';
import { me } from '../store/me.js';
import { resolveComponent } from './componentMap.js';
import {
  MODULE_REGISTRY_VERSION,
  availableBusinessLines,
  canAccessModule,
  getModule,
  modulePath,
  modules,
} from './registry.js';

const required = [
  'module_key', 'version', 'workspace', 'business_line', 'title', 'route',
  'nav_group', 'nav_order', 'component', 'capability', 'scope', 'api_router',
  'enabled', 'feature_flag', 'dependencies', 'migration', 'scheduler', 'tests', 'audit',
];

beforeEach(() => {
  me.value = {
    capabilities: ['pipelines'], scoped_capabilities: { org: ['hangzhou'] },
    regions: ['hangzhou'], all_regions: ['hangzhou', 'shaoxing'], is_admin: false,
  };
});

describe('versioned module registry', () => {
  it('keeps unique keys and complete declarations', () => {
    expect(MODULE_REGISTRY_VERSION).toBe(1);
    expect(new Set(modules.map((item) => `${item.workspace}:${item.module_key}`)).size).toBe(modules.length);
    for (const item of modules) {
      for (const field of required) expect(item, `${item.module_key}.${field}`).toHaveProperty(field);
      expect(() => resolveComponent(item.component)).not.toThrow();
    }
  });

  it('uses stable workspace routes', () => {
    expect(modulePath(getModule('admin', 'data-freshness'))).toBe('/admin/data-freshness');
    expect(modulePath(getModule('business', 'flow'), 'hangzhou')).toBe('/business/hangzhou/flow');
  });

  it('fails closed for unknown modules and components', () => {
    expect(getModule('admin', 'not-registered')).toBeNull();
    expect(canAccessModule(getModule('admin', 'not-registered'))).toBe(false);
    expect(() => resolveComponent('NotRegisteredPage')).toThrow('未注册的前端组件');
    expect(canAccessModule({ ...getModule('admin', 'data-freshness'), enabled: false })).toBe(false);
  });

  it('enforces regional business scope and admin capability', () => {
    const flow = getModule('business', 'flow');
    expect(canAccessModule(flow, 'hangzhou')).toBe(true);
    expect(canAccessModule(flow, 'shaoxing')).toBe(false);
    expect(availableBusinessLines(flow)).toEqual(['hangzhou']);
    expect(canAccessModule(getModule('admin', 'data-freshness'))).toBe(true);
    me.value.capabilities = [];
    expect(canAccessModule(getModule('admin', 'data-freshness'))).toBe(false);
  });
});
