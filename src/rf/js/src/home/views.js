'use strict';

var React = require('react');

var LoginView = React.createBackboneClass({
    render: function() {
        return <div>
            <p>LOGIN</p>
            <p><a href="#" data-url="/logout">Logout</a></p>
        </div>
    }
});

module.exports = {
    LoginView: LoginView
};
