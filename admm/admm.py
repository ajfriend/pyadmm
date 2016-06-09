from collections import defaultdict
from .rho_adjust import rescale_rho_duals
from .timer import Timer, PrintTimer

from .functional import map_apply, unzip, fast_avg

import numpy as np

import matplotlib.pyplot as plt

from numbers import Number

# there is some weird startup cost to this call.
# it was throwing off timing, making the first calculation of residuals
# seemingly take longer than needed.
# calling it once here removes the extra overhead. super weird...
np.linalg.norm([0,.1])

""" the admm algo won't know anything about shared keys.
That happens entirely in the fusion center prox function.
"""

"""
Proxes have the form:

x, info = prox(x0, rho)

where x, x0 are dictionaries with the keys and values
the prox cares about. Proxes should be able to handle
an empty input dict, which would correspond to the proper 0 element.
To do this, they need to know the keys and datashapes they expect to work on.

proxes may maintain state to exploit caching and warm-starting.

info can be {} or None, returning specific solver information, or other
information about the prox computation.
"""

# todo: add a selector for the rho adjustment

def make_xin(xbar, u):
    """ Make the input to the prox function, xbar-u
    assume u has the proper keys
    assume xbar[key] is 0 if key is not in xbar
    assume xbar is a defaultdict that goes to zero
    
    Expect xbar=u={} on the first iteration
    """
    x_in = {}
    
    # todo: this is a weird hack for if u is not present, but xbar is
    if not u:
        for k in xbar:
            x_in[k] = xbar[k]
    else:
        for k in u:
            x_in[k] = xbar[k] - u[k]
        
    return x_in

def update_u(u, x, xbar):
    """update u based on keys in x.
    Modifies u. returns nothing."""
    for k in x:
        u[k] = u[k] + x[k] - xbar[k]

        
def admm_step(proxes, xbar, us, rho, hook=None, mapper=None, rho_adj=None):
    """ Does one ADMM iteration
    - x_i = prox(xbar - u_i)
    - u_i = u_i + x_i _ xbar

    Returns:
    xbar
    us
    info: dict with info, including residuals and timing

    """
    step_info = {}
    step_info['rho'] = rho

    with Timer(step_info, 'total_step'):
        # prep the input to the prox
        with Timer(step_info, 'x_in'):
            xins = [make_xin(xbar, u) for u in us]

        
        # then we prox
        # total time
        # custom info from the proxes
        # built in timing info on each prox
        with Timer(step_info, 'total_proxes'):
            #out = [prox(xin, rho) for xin, prox in zip(xins, proxes)]
            out, step_info['times']['proxes'] = map_apply(proxes, xins,
                                                          rep_args=[rho],
                                                          mapper=mapper)
            xs, step_info['prox_infos'] = unzip(out)
        
        with Timer(step_info, 'xbar'):
            # then we compute xbar
            xbarold = xbar
            xbar = fast_avg(xs)
        
        with Timer(step_info, 'us'):
            # then we update the us
            for u,x in zip(us,xs):
                update_u(u,x,xbar)
            
        # maybe de-mean the us

        with Timer(step_info, 'resid'):
            # compute residuals, update iteration info
            r,s = residuals(xs, xbar, xbarold, rho)
            step_info['r'] = r
            step_info['s'] = s

        # adjust rho?
        with Timer(step_info, 'rho_scaling'):
            rho, us, step_info = do_scaling(rho_adj, step_info, us)

        if hook:
            with Timer(step_info, 'hook'):
                step_info['hook'] = hook(xbar)
        
    return xbar, us, rho, step_info

def admm(proxes, rho, steps=10, hook=None, rho_adj=None):
    xbar = defaultdict(float)
    us = [defaultdict(float) for _ in proxes]

    infos = []

    for _ in range(steps):
        xbar, us, rho, step_info = admm_step(proxes, xbar, us, rho, hook=hook, rho_adj=rho_adj)
    
        infos += [step_info]
    
    return xbar, infos


def do_scaling(scale_func, step_info, us):
    """ Rescale rho (and the us)
    as a result of the residual information (r,s)
    in `step_info`, and the stored rho value in
    `step_info`.

    Modify in place the step_info dict,
    but return it just to make explicit that
    it may be modified
    """
    r,s = step_info['r'], step_info['s']
    rho = step_info['rho']

    if scale_func:
        scale = scale_func(r,s)

    if scale != 1.0:
        rho, us = rescale_rho_duals(rho, us, scale)

    return rho, us, step_info

def get_residuals(infos):
    r = [info['r'] for info in infos]
    s = [info['s'] for info in infos]

    return r, s

def get_info(infos, *names):
    out = (tuple(info[n] for n in names) for info in infos)

    return unzip(out)

def plot_resid(r,s):
    n = len(r)
    plt.semilogy(range(n), r, range(n), s)
    plt.legend(['r', 's'])

def general_residuals(xs, xbar, xbarold, rho):
    """ Compute the residuals for floats or numpy arrays.
    Suffers heavy overhead from np.linalg.norm in the case of all the
    data being floats.
    """
    npnorm = np.linalg.norm
    r = 0.0
    s = 0.0

    for x in xs:
        for k,v in x.items():
            xbark = xbar[k]
            r += npnorm(v - xbark)**2
            s += npnorm(xbark - xbarold[k])**2

    return np.sqrt(r), rho*np.sqrt(s)

def general_residuals2(xs, xbar, xbarold, rho):
    """ Reduces the overhead from `general_residuals()`,
    but not as much as `float_residuals()`.
    """

    npnorm = np.linalg.norm
    r = 0.0
    s = 0.0

    for x in xs:
        for k,v in x.items():
            xbark = xbar[k]

            rval = v - xbark
            sval = xbark - xbarold[k]

            if isinstance(rval, Number):
                r += (rval)**2
                s += (sval)**2
            else:
                r += npnorm(rval)**2
                s += npnorm(sval)**2

    return np.sqrt(r), rho*np.sqrt(s)

def float_residuals(xs, xbar, xbarold, rho):
    """ Compute the residuals when all the values in the dictionaries
    are floats (no numpy arrays allowed).

    Much faster than having to call np.linalg.norm, or check if the values
    are floats.

    XXX: have to update the algorithm to use this by default if eligible
    """
    r = 0.0
    s = 0.0

    for x in xs:
        for k,v in x.items():
            xbark = xbar[k]
            r += (v - xbark)**2
            s += (xbark - xbarold[k])**2

    return np.sqrt(r), rho*np.sqrt(s)

# set the residuals function. float_residuals is fastest (but only OK
# to use when we know all the variables are floats, and not numpy arrays)
residuals = float_residuals


