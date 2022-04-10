/* eslint-disable no-restricted-globals */

/**
 * NOTE: Please avoid adding React as a transitive dependency to this file, as it can break
 * the development workflow. https://github.com/pmmmwh/react-refresh-webpack-plugin/issues/24
 *
 * If you see an error like `$RefreshReg$ is not defined` during development, check the
 * dependencies of this file. If you find that React has been included as a dependency, please
 * try to remove it.
 */

import {layoutAssetGraph} from '../asset-graph/layout';
import {layoutOpGraph} from '../graph/layout';
const ctx: Worker = self as any;

ctx.addEventListener('message', (event) => {
  if (event.data.type === 'layoutOpGraph') {
    const {ops, parentOp} = event.data;
    ctx.postMessage(layoutOpGraph(ops, parentOp));
  } else if (event.data.type === 'layoutAssetGraph') {
    const {graphData} = event.data;
    ctx.postMessage(layoutAssetGraph(graphData));
  }
});
