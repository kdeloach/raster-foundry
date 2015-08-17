'use strict';

var $ = require('jquery'),
    views = require('./views'),
    router = require('../router').router;

var HomeController = {
    index: function() {
        router.navigate('/login', {trigger: true});
    }
};

var UserController = {
    login: function() {
        var view = new views.LoginView();
        $('#container').html(view.render());
    },

    logout: function() {
    }
};

module.exports = {
    HomeController: HomeController,
    UserController: UserController
};
